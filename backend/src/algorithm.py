import math,hashlib
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.model_selection import train_test_split
from sklearn.metrics import adjusted_rand_score

def scaled(points):
    """经纬度按局部米制投影后统一为60米块，水深/3、强度/8，与eps同量纲。"""
    lon0=points[0]['lon'];lat0=points[0]['lat']
    return np.array([[math.radians(p['lon']-lon0)*6371000*math.cos(math.radians(lat0))/60,math.radians(p['lat']-lat0)*6371000/60,p['depth_m']/3,p['strength_db']/8] for p in points])

def analyze(points,eps,min_samples):
    x=scaled(points)
    labels=DBSCAN(eps=eps,min_samples=min_samples).fit_predict(x)
    rng=np.random.default_rng(37);ref=np.vstack([rng.normal(c,.16,(35,4)) for c in [[0,0,2,-5],[4,0,5,-4],[8,0,8,-7]]]+[rng.uniform([-1,-1,0,-12],[10,1,10,-2],(15,4))]);truth=np.repeat([0,1,2,-1],[35,35,35,15])
    train,test=train_test_split(np.arange(120),test_size=.3,random_state=37,stratify=truth)
    sensitivity=[]
    for e in [.8,1.2,1.6]:
        observed=DBSCAN(eps=e,min_samples=min_samples).fit_predict(x);held=DBSCAN(eps=e,min_samples=3).fit_predict(ref[test]);sensitivity.append({'eps':e,'clusters':len(set(observed)-{-1}),'noise':int(sum(observed==-1)),'reference_holdout_ari':float(adjusted_rand_score(truth[test],held))})
    hold=DBSCAN(eps=eps,min_samples=3).fit_predict(ref[test])
    return [{'echo_id':p['id'],'label':int(label)} for p,label in zip(points,labels)],{'clusters':len(set(labels)-{-1}),'noise_count':int(sum(labels==-1)),'point_count':len(points),'reference_holdout_ari':float(adjusted_rand_score(truth[test],hold)),'sensitivity':sensitivity,'reference_train_count':len(train),'reference_holdout_count':len(test),'seed':37,'input_sha256':hashlib.sha256(x.tobytes()).hexdigest(),'source':'实际回波聚类；ARI为独立合成参照的36点留出验证，无监督算法不训练预测模型'}

class _Sets:
    """并查集，同时维护每个根与其他根的异群约束（cannot-link）。"""
    def __init__(self,n):
        self.p=list(range(n));self.size=[1]*n;self.cannot=[set() for _ in range(n)]
    def find(self,i):
        while self.p[i]!=i:
            self.p[i]=self.p[self.p[i]];i=self.p[i]
        return i
    def union(self,i,j):
        a,b=self.find(i),self.find(j)
        if a==b:return a
        if (self.size[a],a)>(self.size[b],b):a,b=b,a
        self.p[b]=a;self.size[a]+=self.size[b]
        merged=self.cannot[a]|self.cannot[b]
        merged.discard(a);merged.discard(b)
        self.cannot[a]=merged;self.cannot[b]=set()
        for other in merged:self.cannot[other].discard(b);self.cannot[other].add(a)
        return a

def _same_adjacency(pairs,index,n):
    adj=[set() for _ in range(n)]
    for a,b in pairs:
        i,j=index[a],index[b];adj[i].add(j);adj[j].add(i)
    return adj

def annotation_conflicts(relations,echo_ids):
    """relations: {(a,b): 'same'|'different'}，返回与同群传递闭包矛盾的异群标注。"""
    ids=sorted(echo_ids);index={v:i for i,v in enumerate(ids)};n=len(ids)
    dsu=_Sets(n);same=[]
    for (a,b),rel in relations.items():
        if a not in index or b not in index:continue
        if rel=='same':dsu.union(index[a],index[b]);same.append((a,b))
    adj=_same_adjacency(same,index,n);conflicts=[]
    for (a,b),rel in sorted(relations.items()):
        if rel!='different' or a not in index or b not in index:continue
        i,j=index[a],index[b]
        if dsu.find(i)!=dsu.find(j):continue
        # 同群链上找出把a、b传成一团的路径，便于主管定位冲突
        previous={i:None};queue=[i];path=[]
        while queue and j not in previous:
            cur=queue.pop(0)
            for nxt in sorted(adj[cur]):
                if nxt not in previous:previous[nxt]=cur;queue.append(nxt)
        if j in previous:
            cur=j
            while cur is not None:path.append(ids[cur]);cur=previous[cur]
            path.reverse()
        conflicts.append({'echo_a_id':min(a,b),'echo_b_id':max(a,b),'kind':'different_within_same_group','same_path':path})
    return conflicts

def _similarity_edges(x,eps):
    n=len(x);edges=[];chunk=512
    for start in range(0,n,chunk):
        block=x[start:start+chunk]
        dist=np.sqrt(((block[:,None,:]-x[None,:,:])**2).sum(axis=2))
        for offset in range(block.shape[0]):
            i=start+offset;row=dist[offset];js=np.nonzero(row<=eps)[0]
            for j in js:
                if j>i:edges.append((float(row[j]),i,int(j)))
    edges.sort()
    return edges

def group_echoes(points,annotations,eps,min_samples):
    """带硬约束的相似度分群。

    - 同群标注先做传递闭包，必须合并；
    - 异群标注禁止两个组以任何方式合并，未标回波不能成为绕行桥梁；
    - 未标回波按eps内的四维相似度单链连接；
    - 不足min_samples且不含标注的组记为噪点。
    所有候选边按(距离,回波序号)的固定全序合并，标注录入顺序不影响结果。
    """
    ids=[p['id'] for p in points];index={eid:i for i,eid in enumerate(ids)};n=len(ids)
    same=[];different=[];annotated=set()
    for item in annotations:
        a,b,rel=int(item['echo_a_id']),int(item['echo_b_id']),item['relation']
        if a not in index or b not in index:raise ValueError('标注引用了不属于该航段的回波')
        if a==b:raise ValueError('不能把回波与自身标注')
        annotated.add(a);annotated.add(b)
        (same if rel=='same' else different).append((index[a],index[b]))
    dsu=_Sets(n)
    for i,j in sorted(same):dsu.union(i,j)
    for i,j in sorted(different):
        a,b=dsu.find(i),dsu.find(j)
        if a==b:raise ValueError('标注存在同群与异群矛盾，请退回整批并修正')
        dsu.cannot[a].add(b);dsu.cannot[b].add(a)
    for distance,i,j in _similarity_edges(scaled(points),eps):
        a,b=dsu.find(i),dsu.find(j)
        if a==b or b in dsu.cannot[a]:continue
        dsu.union(a,b)
    roots={}
    for i in range(n):roots.setdefault(dsu.find(i),[]).append(i)
    ordered=sorted(roots.values(),key=lambda members:min(ids[i] for i in members))
    label_of={};groups=[];assignments=[]
    next_label=0
    for members in ordered:
        member_ids=[ids[i] for i in members]
        has_annotation=any(eid in annotated for eid in member_ids)
        if len(members)<min_samples and not has_annotation:
            label=-1
        else:
            label=next_label;next_label+=1
            along=[points[i]['along_m'] for i in members]
            groups.append({'label':label,'echo_ids':member_ids,'size':len(members),'annotated':has_annotation,'along_min':min(along),'along_max':max(along),'mean_depth_m':round(float(np.mean([points[i]['depth_m'] for i in members])),2)})
        for i in members:label_of[ids[i]]=label
    noise_count=sum(1 for v in label_of.values() if v==-1)
    for eid in ids:assignments.append({'echo_id':eid,'label':label_of[eid]})
    metrics={'groups':len(groups),'noise_count':noise_count,'point_count':n,'annotated_echo_count':len(annotated),'annotation_count':len(annotations),'eps':eps,'min_samples':min_samples,'source':'按回波四维相似度分群；同群传递合并，异群禁止经未标回波绕行合并'}
    return {'assignments':assignments,'groups':groups,'metrics':metrics}
