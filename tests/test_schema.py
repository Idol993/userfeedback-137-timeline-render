from media.api import ExportRequest
def test_request_aliases():
    value=ExportRequest.model_validate({'clips':[{'path':'clip.mp4','in':0.5,'out':2.5}],'transition_seconds':[]})
    assert value.model_dump(by_alias=True)['clips'][0]['in']==0.5
