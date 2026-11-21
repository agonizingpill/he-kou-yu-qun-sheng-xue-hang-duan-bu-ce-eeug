import sys, itertools
sys.path.insert(0,'/workspace/backend')
from src.algorithm import group_echoes, annotation_conflicts

DEG_PER_M=1.0/96_500  # 30°N处1米约合经度度数；投影后除以60米缩放
def pt(i, along, depth=10.0, strength=-45.0):
    return {'id':i,'along_m':along,'lon':120.1+along*DEG_PER_M,'lat':30.1,'depth_m':depth,'strength_db':strength}

def by_group(result):
    out={}
    for a in result['assignments']:out.setdefault(a['label'],[]).append(a['echo_id'])
    return {k:sorted(v) for k,v in out.items() if k>=0}

# 1. 同群传递：1~2, 2~3 => 1,2,3 同组；4 远离
points=[pt(1,100),pt(2,102),pt(3,104),pt(4,400)]
r=group_echoes(points,[{'echo_a_id':1,'echo_b_id':2,'relation':'same'},{'echo_a_id':2,'echo_b_id':3,'relation':'same'}],eps=0.1,min_samples=9)
assert by_group(r)=={0:[1,2,3]}, by_group(r)
print('PASS same transitivity')

# 2. 异群隔着未标回波也不能绕行并群：1--3--2 相似链，但1与2标异群
points=[pt(1,100),pt(2,140),pt(3,120)]
r=group_echoes(points,[{'echo_a_id':1,'echo_b_id':2,'relation':'different'}],eps=0.5,min_samples=1)
g=by_group(r)
assert not any(1 in v and 2 in v for v in g.values()), g
print('PASS cannot-link cannot bridge through unannotated echo',g)

# 3. 空间相近但标异群，硬约束阻止相似度合并
points=[pt(1,100,depth=10),pt(2,101,depth=10),pt(3,102,depth=10),pt(4,103,depth=10)]
r=group_echoes(points,[{'echo_a_id':1,'echo_b_id':4,'relation':'different'}],eps=5,min_samples=1)
g=by_group(r)
assert not any(1 in v and 4 in v for v in g.values()), g
print('PASS hard cannot-link despite similarity')

# 4. 录入顺序无关：同一集合标注随机排列，结果一致
base=[(1,2,'same'),(2,3,'same'),(4,5,'same'),(3,4,'different')]
points=[pt(i,100+i*20,depth=10+(i%3)) for i in range(1,8)]
results=set()
for perm in itertools.permutations(base):
    rr=group_echoes(points,[{'echo_a_id':a,'echo_b_id':b,'relation':c} for a,b,c in perm],eps=0.5,min_samples=1)
    results.add(tuple(sorted((a['echo_id'],a['label']) for a in rr['assignments'])))
assert len(results)==1, results
print('PASS order independence')

# 5. 自我标注、未知回波
for bad in ([{'echo_a_id':1,'echo_b_id':1,'relation':'same'}],[{'echo_a_id':1,'echo_b_id':99,'relation':'same'}]):
    try:group_echoes(points,bad,eps=3,min_samples=1);raise AssertionError('should reject')
    except ValueError:pass
print('PASS invalid annotation rejected')

# 6. 矛盾检测：1-2同,2-3同,1-3异 => 冲突并给出传递路径
c=annotation_conflicts({(1,2):'same',(2,3):'same',(1,3):'different'},{1,2,3})
assert c and c[0]['kind']=='different_within_same_group' and c[0]['same_path']==[1,2,3], c
print('PASS conflict path',c)

# 7. 方向对称：a,b 与 b,a 结果一致
r1=group_echoes(points,[{'echo_a_id':5,'echo_b_id':6,'relation':'same'}],eps=3,min_samples=1)
r2=group_echoes(points,[{'echo_a_id':6,'echo_b_id':5,'relation':'same'}],eps=3,min_samples=1)
assert r1['assignments']==r2['assignments'] and r1['groups']==r2['groups']
print('PASS symmetric pair canonicalization')

# 8. 未标回波按相似度分群；含标注的小组即使少于min_samples也保留
points=[pt(1,100,10,-45),pt(2,100.2,10.1,-45),pt(3,900,25,-70)]
r=group_echoes(points,[{'echo_a_id':1,'echo_b_id':2,'relation':'same'}],eps=0.1,min_samples=5)
g=by_group(r)
assert g=={0:[1,2]}, g
assert r['assignments'][2]['label']==-1
print('PASS similarity grouping + small annotated group kept')
print('ALL ALGORITHM TESTS PASSED')
