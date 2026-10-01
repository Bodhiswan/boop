"""Build verifiable source references from server-owned real context."""
import hashlib
import json
import re

FIELDS={'sleep':('rest','sleep'),'effort':('effort',),'recovery':('hrv','resting_hr','charge','respiration','skin_temperature')}
PATTERN=re.compile(r'\[(E[0-9a-f]{8})\]')


def evidence_catalog(snapshots):
    catalog={}
    for snapshot in snapshots:
        context=snapshot['data']
        for row in context.get('recent_days_oldest_first',[]):
            for topic in snapshot['topics']:
                if topic not in FIELDS:continue
                values={key:row[key] for key in FIELDS[topic] if key in row}
                if not values:continue
                payload={'day':row.get('day'),'topic':topic,'values':values}
                digest=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()[:8]
                catalog['E'+digest]={'id':'E'+digest,**payload,'coverage':row.get('coverage',{}),
                    'source':'Real BOOP observations and source-labelled local estimates'}
        if 'live' in snapshot['topics'] and context.get('live'):
            payload={'day':context.get('observed_at'),'topic':'live','values':context['live']}
            digest=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()[:8]
            catalog['E'+digest]={'id':'E'+digest,**payload,'source':'Real live strap snapshot'}
    return list(catalog.values())


def referenced_evidence(answer,catalog):
    allowed={item['id']:item for item in catalog}
    mentioned=list(dict.fromkeys(PATTERN.findall(answer)))
    unknown=[key for key in mentioned if key not in allowed]
    clean=PATTERN.sub(lambda match:match.group(0) if match.group(1) in allowed else '',answer)
    cited=[allowed[key] for key in mentioned if key in allowed]
    return clean,cited,unknown
