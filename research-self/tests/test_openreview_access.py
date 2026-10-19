import httpx
import pytest

from paper_ingest.openreview import OpenReview


class Client:
    def __init__(self): self.calls=[]
    async def get(self,url,params):
        self.calls.append(dict(params))
        if len(self.calls)==1:
            request=httpx.Request("GET",url)
            response=httpx.Response(403,request=request)
            raise httpx.HTTPStatusError("forbidden",request=request,response=response)
        request=httpx.Request("GET",url)
        return httpx.Response(200,json={"notes":[]},request=request)
    async def aclose(self): pass

@pytest.mark.asyncio
async def test_private_invitation_403_retries_only_public_venue_id():
    client=Client(); api=OpenReview(client)
    assert [item async for item in api.notes("ICLR.cc/2025/Conference/-/Submission")] == []
    assert client.calls[0]["invitation"]=="ICLR.cc/2025/Conference/-/Submission"
    assert client.calls[1]["content.venueid"]=="ICLR.cc/2025/Conference/Submission"
