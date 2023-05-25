from flask import Flask, request, jsonify
from datetime import datetime
from os import path
import sqlite3
import threading
from time import sleep
from concurrent.futures import ThreadPoolExecutor
from collections import namedtuple
from flask_cors import CORS

from seqinfo import SeqInfo

DB_DIR = 'gmsc-db'
DB_PATH = 'gmsc-db/gmsc10hq.sqlite3'
if path.exists(DB_PATH):
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    seqinfo90 = SeqInfo( '90AA')
    IS_DEMO = False
else:
    import sys
    sys.stderr.write(f'WARNING: Database file {DB_PATH} not found\n')
    sys.stderr.write(f'WARNING: Using demo database\n')
    con = sqlite3.connect('gmsc10-demo.sqlite3', check_same_thread=False)
    IS_DEMO = True

app = Flask('GMSC')
CORS(app)


@app.get("/v1/seq-info/<seq_id>")
def get_seq_info(seq_id):
    tokens = seq_id.split(".")
    if len(tokens) != 3:
        return {"error": "Invalid sequence ID (only 'GMSC10' database supported)"}, 400
    (db, sample_type, seq_ix) = tokens
    if db != 'GMSC10':
        return {"error": "Invalid sequence ID"}, 400
    if sample_type not in ('100AA', '90AA'):
        return {"error": "Invalid sequence ID (seq type must be '90AA' or '100AA')"}, 400
    if IS_DEMO:
        from demo import get_demo_seqinfo
        return get_demo_seqinfo(int(seq_ix))
    return seqinfo90.get_seqinfo(seq_id)


@app.post("/v1/seq-info-multi/")
def get_seq_info_multi():
    entries = request.json.get('seq_ids')
    if entries is None:
        return {"error": "Missing seq_ids parameter"}, 400
    if len(entries) > 100:
        return {"error": "Too many seq_ids"}, 400
    return [seqinfo90.get_seqinfo(e) for e in entries]


def parse_bool(s : str):
    s = s.lower()
    if s in ('true', '1', 'yes'):
        return True
    if s in ('false', '0', 'no'):
        return False
    return None

@app.route('/v1/seq-filter/', methods=['POST'])
def get_seq_filter():
    cur = con.cursor()
    hq_only = request.form.get('hq_only', False)
    habitat = request.form.get('habitat')
    if habitat is None:
        return {"status": "error", "msg": "Invalid habitat value"}, 400
    taxonomy = request.form.get('taxonomy')
    if taxonomy is not None :
        if '__' not in taxonomy:
            return {"status": "error", "msg": "Invalid taxonomy value"}, 400
    results = seqinfo90.seq_filter(parse_bool(hq_only), habitat, taxonomy)
    return jsonify({
        "status": "Ok",
        "results": results,
        })

class SearchIDGenerator:
    def __init__(self):
        self.next_id = 0
    def get_next_id(self):
        import random
        from string import ascii_lowercase
        randstr = ''.join(random.choice(ascii_lowercase) for i in range(4))
        self.next_id += 1
        return f"{self.next_id}-{randstr}"

    @classmethod
    def get_index(cls, search_id : str) -> int:
        ix, _ = search_id.split("-")
        return int(ix)

    def get_cur_index(self) -> int:
        return self.next_id

searches = {}
next_search_id = SearchIDGenerator()
search_lock = threading.Lock()
searcher = ThreadPoolExecutor(2)
SearchObject = namedtuple("SearchObject", ["start_time", "future"])

def parse_gmsc_mapper_results(basedir):
    import pandas as pd
    alignment = pd.read_table(f'{basedir}/alignment.out.smorfs.tsv', header=None)
    habitat = pd.read_table(f'{basedir}/habitat.out.smorfs.tsv', index_col=0)
    quality = pd.read_table(f'{basedir}/quality.out.smorfs.tsv', index_col=0)
    taxonomy = pd.read_table(f'{basedir}/taxonomy.out.smorfs.tsv', index_col=0)
    meta = pd.concat([habitat, quality, taxonomy], axis=1)
    alignment.columns = 'qseqid,sseqid,full_qseq,full_sseq,qlen,slen,length,qstart,qend,sstart,send,bitscore,pident,evalue,qcovhsp,scovhsp'.split(',')

    result = meta.to_dict('index')

    for k in result.keys():
        loc = alignment.query('qseqid == @k')
        result[k]['aminoacid'] = loc.head(1).full_qseq.values[0]
        result[k]['hits'] = \
                loc[['sseqid', 'evalue', 'pident']].rename(columns=
                    {'sseqid': 'id',
                    'pident': 'identity'},).to_dict('records')
    return result

def do_search(seqdata):
    import tempfile
    import subprocess
    with tempfile.TemporaryDirectory() as tdir:
        if not path.exists(DB_DIR):
            sleep(10)
            return parse_gmsc_mapper_results('./demo_gmsc_mapper_output')
        with open(path.join(tdir, "seqs.faa"), "w") as f:
            f.write(seqdata)
        sleep(1)
        subprocess.check_call(
                ['gmsc-mapper',
                 '--aa-genes', path.join(tdir, "seqs.faa"),
                 '-o', path.join(tdir, "output"),
                 '--threads', '12',
                 '--db', f'{DB_DIR}/90AA_GMSC.dmnd',
                 '--habitat', f'{DB_DIR}/90AA_habitat.npy',
                 '--habitat-index', f'{DB_DIR}/90AA_ref_multi_general_habitat_index.tsv',
                 '--quality', f'{DB_DIR}/90AA_highquality.txt.gz',
                 '--taxonomy', f'{DB_DIR}/90AA_taxonomy.npy',
                 '--taxonomy-index', f'{DB_DIR}/90AA_ref_taxonomy_index.tsv',
                 ],
                )

        return parse_gmsc_mapper_results(path.join(tdir, "output"))


@app.route('/internal/seq-search/', methods=['POST'])
def seq_search():
    now = datetime.now()
    seqdata = request.form.get('sequence_faa')
    with search_lock:
        sid = next_search_id.get_next_id()
        searches[sid] = SearchObject(now, searcher.submit(do_search, seqdata))
    return jsonify({
        "search_id": sid,
        "status": "Ok",
        })

@app.get('/internal/seq-search/<search_id>')
def seq_search_results(search_id):
    if search_id not in searches:
        return {"error": "Invalid search ID"}, 400
    sdata = searches[search_id].future
    if not sdata.done():
        sleep(1)
        return {"search_id": search_id, "status": "Running"}
    r = sdata.result()
    return jsonify({
        "search_id": search_id,
        "status": "Done",
        "results": r
        })
