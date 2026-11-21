import hashlib,json
import redis
from fastapi import HTTPException
from sqlalchemy import select
from .models import Echo,AnnotationState,EchoAnnotation,AnnotationBatch,SchoolGrouping,GroupingReview,Audit
from .common import get,reject,finish,transect_lock,dump
from .school_service import queue
from .algorithm import annotation_conflicts,group_echoes

RELATIONS={'same','different'}

def payload_sha256(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()

def canonical_pairs(pairs):
    """把一批标注规范化为(a<b, relation)的固定顺序；录入顺序不进入任何结果。"""
    seen={}
    for pair in pairs:
        a,b=int(pair['echo_a_id']),int(pair['echo_b_id'])
        if a==b:raise HTTPException(422,'不能把回波与自身标注')
        relation=pair['relation']
        if relation not in RELATIONS:raise HTTPException(422,'标注关系只能是同群 same 或异群 different')
        key=(min(a,b),max(a,b))
        if key in seen and seen[key]!=relation:
            raise HTTPException(409,{'message':f'同一批里回波{key[0]}与{key[1]}既标同群又标异群，整批退回','conflicts':[{'echo_a_id':key[0],'echo_b_id':key[1],'kind':'same_and_different_in_batch'}]})
        seen[key]=relation
    return [{'echo_a_id':a,'echo_b_id':b,'relation':seen[(a,b)]} for a,b in sorted(seen)]

def snapshot(db,t):
    state=db.scalar(select(AnnotationState).where(AnnotationState.transect_id==t.id))
    rows=db.scalars(select(EchoAnnotation).where(EchoAnnotation.transect_id==t.id).order_by(EchoAnnotation.echo_a_id,EchoAnnotation.echo_b_id)).all()
    annotations=[{'echo_a_id':r.echo_a_id,'echo_b_id':r.echo_b_id,'relation':r.relation} for r in rows]
    return (state.version if state else 0),annotations

def annotate(db,transect_id,pairs,request_key,user):
    c,t=transect_lock(db,transect_id)
    batch=canonical_pairs(pairs);digest=payload_sha256(batch)
    if request_key:
        seen=db.scalar(select(AnnotationBatch).where(AnnotationBatch.transect_id==t.id,AnnotationBatch.request_key==request_key))
        if seen:
            reject(seen.payload_sha256!=digest,'该请求键已用于另一批标注，拒绝复用')
            return seen.response
    echo_ids=set(db.scalars(select(Echo.id).where(Echo.transect_id==t.id)))
    for row in batch:
        if row['echo_a_id'] not in echo_ids or row['echo_b_id'] not in echo_ids:
            raise HTTPException(409,{'message':'标注引用了不属于该航段的回波，整批退回','conflicts':[{'echo_a_id':row['echo_a_id'],'echo_b_id':row['echo_b_id'],'kind':'unknown_echo'}]})
    state=db.scalar(select(AnnotationState).where(AnnotationState.transect_id==t.id).with_for_update())
    if state is None:
        state=AnnotationState(transect_id=t.id,version=0);db.add(state);db.flush()
    rows=db.scalars(select(EchoAnnotation).where(EchoAnnotation.transect_id==t.id)).all()
    current={(r.echo_a_id,r.echo_b_id):r.relation for r in rows}
    merged=dict(current)
    for row in batch:merged[(row['echo_a_id'],row['echo_b_id'])]=row['relation']
    conflicts=annotation_conflicts(merged,echo_ids)
    if conflicts:
        raise HTTPException(409,{'message':'标注同群与异群矛盾，整批退回，未登记任何标注','conflicts':conflicts})
    changed={key:relation for key,relation in merged.items() if current.get(key)!=relation}
    if changed:
        for key,relation in sorted(changed.items()):
            existing=next((r for r in rows if (r.echo_a_id,r.echo_b_id)==key),None)
            if existing is None:db.add(EchoAnnotation(transect_id=t.id,echo_a_id=key[0],echo_b_id=key[1],relation=relation,actor=user.username))
            else:
                existing.relation=relation;existing.actor=user.username
        state.version+=1
        db.add(Audit(actor=user.username,action='登记回波同群异群标注',detail=f'transect={t.id} v{state.version} changed={len(changed)}'))
        response={'transect_id':t.id,'annotation_version':state.version,'annotations':[{'echo_a_id':a,'echo_b_id':b,'relation':r} for (a,b),r in sorted(merged.items())],'changed':True}
    else:
        # 断线重试或重复提交同一批标注：不新增标注、不推进版本、不重复登记。
        response={'transect_id':t.id,'annotation_version':state.version,'annotations':[{'echo_a_id':a,'echo_b_id':b,'relation':r} for (a,b),r in sorted(merged.items())],'changed':False}
    db.add(AnnotationBatch(transect_id=t.id,request_key=request_key,payload_sha256=digest,annotation_version=response['annotation_version'],response=response,actor=user.username))
    db.commit();return response

def enqueue(db,transect_id,eps,min_samples,request_key,user):
    c,t=transect_lock(db,transect_id)
    reject(db.scalar(select(Echo.id).where(Echo.transect_id==t.id).limit(1)) is None,'该航段尚无回波，先记录探头数据')
    request={'transect_id':t.id,'data_version':t.data_version,'eps':eps,'min_samples':min_samples}
    digest=payload_sha256(request)
    if request_key:
        seen=db.scalar(select(SchoolGrouping).where(SchoolGrouping.transect_id==t.id,SchoolGrouping.request_key==request_key))
        if seen:
            reject(seen.payload_sha256!=digest,'该请求键已用于另一次重算，拒绝复用')
            return dump(seen)
    version,annotations=snapshot(db,t)
    row=SchoolGrouping(transect_id=t.id,data_version=t.data_version,annotation_version=version,eps=eps,min_samples=min_samples,annotations=annotations,request_key=request_key,payload_sha256=digest)
    db.add(row);db.flush();result=finish(db,user,'按标注与相似度重算鱼群分群',row)
    try:queue.rpush('estuary:groupings',row.id)
    except redis.RedisError:
        row.status='failed';row.error='队列暂不可用，恢复后可重新提交';db.commit();raise HTTPException(503,row.error)
    return result

def review(db,id,noise_note,decision,user):
    initial=get(db,SchoolGrouping,id);c,t=transect_lock(db,initial.transect_id);row=get(db,SchoolGrouping,id,True)
    reject(row.status!='done','仅已完成的分群重算可签字')
    version,_=snapshot(db,t)
    # 请求提交后标注又被修改时，本次结果基于旧标注，晚算完也不能签字。
    reject(row.data_version!=t.data_version or row.annotation_version!=version,'标注或回波已更新，该结果基于旧版本，不能签字，请重新计算')
    reject(row.metrics.get('noise_count',0)>0 and len(noise_note.strip())<8,'存在噪点，请补充至少8字现场解释')
    row.status='reviewed';t.status='reviewed';review_row=GroupingReview(grouping_id=row.id,noise_note=noise_note,decision=decision,annotation_version=version);db.add(review_row);db.flush();return finish(db,user,'签署鱼群分群重算结果',review_row)
