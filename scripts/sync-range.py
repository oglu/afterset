#!/usr/bin/env python3
"""Run and wait for the same sync jobs exposed through MCP."""
import argparse
import json
import time
from afterset import store, sync

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--start', required=True)
parser.add_argument('--end', required=True)
parser.add_argument('--scope', choices=['activities', 'wellness'], default='activities')
parser.add_argument('--enrich', action='store_true')
args = parser.parse_args()
store.initialize()
job = sync.start_sync(args.start, args.end, args.scope, args.enrich)
print(json.dumps(job), flush=True)
previous = None
while True:
    result = sync.job_status(job['job_id'])
    compact = {'state':result['state'], 'progress':result['progress']}
    if compact != previous:
        print(json.dumps(compact), flush=True)
        previous = compact
    if result['state'] not in ('queued', 'running'):
        raise SystemExit(0 if result['state'] == 'completed' else 1)
    time.sleep(3)
