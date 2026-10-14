"""Build the lookup files behind the Overview page's Transaction Overview and
Linked Account Network cards, so the app never scans the big CSVs on a
live request. Same idea as precompute.py: do the expensive read once,
offline, and save small files the Flask app loads instantly.

Reads (all 5,078,336 rows, verified row-aligned before anything is saved):
  data/cleaned_transactions.csv         real amounts, bank IDs, currencies,
                                        preprocessing flags, label
  results/final_ensemble_risk_scores.csv  Risk Engine score + level
                                        (its own amount columns are NOT real
                                        amounts, so they are not used)
  featured/featured_with_graph.csv      the Graph notebook's community +
                                        mule tags (assigned per SENDER)

Writes:
  data/precomputed/transactions_overview.parquet  one row per transaction
  data/precomputed/account_graph_flags.json       account -> community / mule,
                                              plus suggested accounts to explore

Transaction ID = 1-based row number (the files have no ID column).

Run once from the project root:
    python precompute_transactions.py
"""
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEANED = os.path.join(ROOT, 'data', 'cleaned_transactions.csv')
RISK = os.path.join(ROOT, 'results', 'final_ensemble_risk_scores.csv')
GRAPH = os.path.join(ROOT, 'data', 'featured', 'featured_with_graph.csv')
OUT_DIR = os.path.join(ROOT, 'data', 'precomputed')
OUT_PARQUET = os.path.join(OUT_DIR, 'transactions_overview.parquet')
OUT_FLAGS = os.path.join(OUT_DIR, 'account_graph_flags.json')
CHUNK = 1_000_000
SUGGESTIONS = 12

CLEANED_COLS = {
    'Sender_Account_Encoded': ('sender', 'int32'),
    'Receiver_Account_Encoded': ('receiver', 'int32'),
    'Sender Bank ID': ('sender_bank', 'int32'),
    'Receiver Bank ID': ('receiver_bank', 'int32'),
    'Amount Paid': ('amount_paid', 'float64'),
    'Amount Received': ('amount_received', 'float64'),
    'Payment_Currency_Encoded': ('pay_cur', 'int8'),
    'Receiving_Currency_Encoded': ('recv_cur', 'int8'),
    'is_self_transfer': ('self_transfer', 'int8'),
    'is_cross_bank': ('cross_bank', 'int8'),
    'is_currency_mismatch': ('currency_mismatch', 'int8'),
    'Is Laundering': ('label', 'int8'),
}
RISK_COLS = ['Sender Account', 'Receiver Account', 'risk_score_100', 'risk_level']
GRAPH_COLS = ['Sender_Account_Encoded', 'Receiver_Account_Encoded',
              'graph_community_id', 'is_suspicious_community', 'graph_is_mule']

os.makedirs(OUT_DIR, exist_ok=True)

readers = (
    pd.read_csv(CLEANED, usecols=list(CLEANED_COLS), chunksize=CHUNK),
    pd.read_csv(RISK, usecols=RISK_COLS, chunksize=CHUNK),
    pd.read_csv(GRAPH, usecols=GRAPH_COLS, chunksize=CHUNK),
)
tx_parts, graph_parts, n = [], [], 0
print('reading cleaned_transactions + risk scores + graph tags ...')
for cl, rk, gr in zip(*readers):
    if not (len(cl) == len(rk) == len(gr)):
        sys.exit(f'row counts differ in the chunk starting at row {n} — files are not aligned')
    s, r = cl['Sender_Account_Encoded'].to_numpy(), cl['Receiver_Account_Encoded'].to_numpy()
    for name, other in (('final_ensemble_risk_scores', rk[['Sender Account', 'Receiver Account']]),
                        ('featured_with_graph', gr[['Sender_Account_Encoded', 'Receiver_Account_Encoded']])):
        o = other.to_numpy()
        if not ((o[:, 0] == s) & (o[:, 1] == r)).all():
            sys.exit(f'{name}.csv is not row-aligned with cleaned_transactions.csv near row {n}')
    part = pd.DataFrame({new: cl[old].astype(dt).to_numpy() for old, (new, dt) in CLEANED_COLS.items()})
    part['risk_score'] = rk['risk_score_100'].astype('float32').to_numpy()
    part['risk_level'] = rk['risk_level'].to_numpy()
    tx_parts.append(part)
    graph_parts.append(pd.DataFrame({
        'sender': s.astype('int32'),
        'community': gr['graph_community_id'].fillna(-1).astype('int32').to_numpy(),
        'suspicious': gr['is_suspicious_community'].fillna(0).astype('int8').to_numpy(),
        'mule': gr['graph_is_mule'].fillna(0).astype('int8').to_numpy(),
        'label': part['label'].to_numpy(),
    }))
    n += len(cl)
    print(f'  … {n:,} rows')

tx = pd.concat(tx_parts, ignore_index=True)
tx['risk_level'] = tx['risk_level'].astype('category')
tx.to_parquet(OUT_PARQUET, index=False, compression='zstd')
print(f'wrote {OUT_PARQUET}  ({os.path.getsize(OUT_PARQUET) / 1_048_576:.1f} MB, {len(tx):,} rows)')

# ---- per-account community + mule tags (the notebook assigns both via the sender)
g = pd.concat(graph_parts, ignore_index=True)
acc = g.groupby('sender').agg(community=('community', 'max'), suspicious=('suspicious', 'max'),
                              mule=('mule', 'max'), txns=('label', 'size'), laundering=('label', 'sum'))
tagged = acc[(acc['community'] >= 0) | (acc['mule'] == 1)]
accounts = {str(a): [int(r.community) if r.community >= 0 else None, int(r.mule)]
            for a, r in tagged.iterrows()}

# Suggested accounts to explore: the most laundering-heavy account of each
# suspicious community (so the list spans many different communities).
sus = acc[acc['suspicious'] == 1].sort_values(['laundering', 'txns'], ascending=False)
picks = sus.groupby('community', sort=False).head(1).head(SUGGESTIONS)
suggestions = [dict(account=int(a), community=int(r.community), mule=bool(r.mule),
                    txns=int(r.txns), laundering=int(r.laundering)) for a, r in picks.iterrows()]

with open(OUT_FLAGS, 'w') as fh:
    json.dump(dict(accounts=accounts, suggestions=suggestions,
                   source='featured/featured_with_graph.csv (Graph Network Analysis notebook)'),
              fh, separators=(',', ':'))
print(f'wrote {OUT_FLAGS}  ({len(accounts):,} tagged accounts, '
      f'{int(acc["mule"].sum())} mule accounts, {int((acc["suspicious"] == 1).sum())} in suspicious communities)')
print('DONE')
