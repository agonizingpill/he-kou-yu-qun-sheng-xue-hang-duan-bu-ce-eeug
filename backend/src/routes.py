import hmac
from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from .database import session
from .models import *
from .schemas import *
from .auth import current,editor,supervisor,issue,password_hash
from .common import dump,get
from . import navigation_service as nav,transect_service as transect,school_service as school,grouping_service as grouping,biology_service as biology,resurvey_service as resurvey,release_service as release
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,db=Depends(session)):
    row=db.scalar(select(User).where(User.username==data.username))
    if not row or not hmac.compare_digest(row.password,password_hash(data.password)):raise HTTPException(401,'账号或密码错误')
    return {'token':issue(row),'username':row.username,'role':row.role}
@router.get('/workspace')
def workspace(db=Depends(session),user=Depends(current)):
    models=[Boat,Cruise,NavigationClosure,Transect,Echo,SchoolAnalysis,SchoolReview,AnnotationState,EchoAnnotation,SchoolGrouping,GroupingReview,Net,NetCalibration,BioSample,MissingInterval,Resurvey,SurveyRelease]
    return {m.__tablename__:[dump(r) for r in db.scalars(select(m).order_by(m.id))] for m in models}
@router.post('/cruises')
def create(data:CruiseInput,db=Depends(session),user=Depends(editor)):return nav.create(db,data,user)
@router.patch('/cruises/{id}/schedule')
def schedule(id:int,data:Schedule,db=Depends(session),user=Depends(editor)):return nav.change(db,id,data,user)
@router.post('/closures')
def closure(data:ClosureInput,db=Depends(session),user=Depends(supervisor)):return nav.closure(db,data,user)
@router.post('/cruises/{id}/launch')
def launch(id:int,db=Depends(session),user=Depends(supervisor)):return nav.launch(db,id,user)
@router.post('/cruises/{id}/return')
def returned(id:int,db=Depends(session),user=Depends(supervisor)):return nav.return_port(db,id,user)
@router.post('/transects')
def create_transect(data:TransectInput,db=Depends(session),user=Depends(editor)):return transect.create(db,data,user)
@router.post('/transects/{id}/echoes')
def echoes(id:int,data:EchoInput,db=Depends(session),user=Depends(editor)):return transect.record(db,id,[p.model_dump() for p in data.points],user)
@router.post('/transects/{id}/simulate')
def simulate(id:int,db=Depends(session),user=Depends(editor)):return transect.simulate(db,id,user)
@router.post('/schools')
def analyze(data:AnalysisInput,db=Depends(session),user=Depends(editor)):return school.enqueue(db,data,user)
@router.post('/schools/{id}/review')
def review(id:int,data:ReviewInput,db=Depends(session),user=Depends(supervisor)):return school.review(db,id,data,user)
@router.post('/annotations')
def annotations(data:AnnotationInput,db=Depends(session),user=Depends(supervisor)):return grouping.annotate(db,data.transect_id,[p.model_dump() for p in data.annotations],data.request_key,user)
@router.post('/groupings')
def groupings(data:GroupingInput,db=Depends(session),user=Depends(supervisor)):return grouping.enqueue(db,data.transect_id,data.eps,data.min_samples,data.request_key,user)
@router.post('/groupings/{id}/review')
def grouping_review(id:int,data:GroupingReviewInput,db=Depends(session),user=Depends(supervisor)):return grouping.review(db,id,data.noise_note,data.decision,user)
@router.post('/calibrations')
def calibration(data:CalibrationInput,db=Depends(session),user=Depends(supervisor)):return biology.calibrate(db,data,user)
@router.post('/samples')
def sample(data:SampleInput,db=Depends(session),user=Depends(editor)):return biology.sample(db,data,user)
@router.post('/gaps')
def gap(data:GapInput,db=Depends(session),user=Depends(editor)):return resurvey.gap(db,data,user)
@router.post('/resurveys')
def plan(data:ResurveyInput,db=Depends(session),user=Depends(supervisor)):return resurvey.plan(db,data,user)
@router.post('/resurveys/{id}/complete')
def complete(id:int,data:CompletionInput,db=Depends(session),user=Depends(supervisor)):return resurvey.complete(db,id,data,user)
@router.post('/resurveys/{id}/cancel')
def cancel(id:int,db=Depends(session),user=Depends(supervisor)):return resurvey.cancel(db,id,user)
@router.post('/releases')
def freeze(data:ReleaseInput,db=Depends(session),user=Depends(supervisor)):return release.freeze(db,data,user)
@router.get('/releases/{id}')
def frozen(id:int,db=Depends(session),user=Depends(current)):return dump(get(db,SurveyRelease,id))
@router.get('/audit')
def audit(db=Depends(session),user=Depends(supervisor)):return [dump(v) for v in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))]
