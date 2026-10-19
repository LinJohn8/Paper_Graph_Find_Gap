"""Bounded official SDK probe of publicly readable historical metadata."""
import json
from pathlib import Path
import openreview
from paper_ingest import db


def main():
    client=openreview.api.OpenReviewClient(baseurl='https://api2.openreview.net')
    request=client.session.request
    client.session.request=lambda *a,**kw:request(*a,**{**kw,'timeout':25})
    rows=[]
    for name,call in [('historical_group',lambda:client.get_group('ICLR.cc/2024/Conference')),('public_single_note',lambda:client.get_note('jYmGi1175R')),('public_notes_batch',lambda:client.get_notes(content={'venueid':'ICLR.cc/2024/Conference'},limit=2))]:
        try:
            value=call()
            rows.append({'check':name,'status':'success','count':len(value) if isinstance(value,list) else 1})
        except Exception as e:
            rows.append({'check':name,'status':'failed','error':str(e)[:1200]})
    result={'checked_at':db.now(),'sdk':'openreview-py','checks':rows}
    path=db.DATA_ROOT/'logs/openreview-sdk-diagnostic.json'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
