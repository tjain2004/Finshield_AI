# ================================================================
#  FinShield AI  —  Flask Backend
#  Anti-Money-Laundering Intelligence Platform
#
#  This file discovers and loads EVERYTHING your notebooks produced:
#    • every .pkl / .pt model file in  models/
#    • every .pkl in  featured/  and  preprocessed/
#    • every .csv in  results/   (small ones eagerly, huge ones on demand)
#    • every .png in  charts/  and  charts/eda_charts/
#
#  Folder layout expected (this file lives at the project root):
#    FinShieldAI/
#      app.py                 <- this file
#      templates/dashboard.html
#      static/dashboard.js, chart.umd.min.js
#      charts/ ... models/ ... results/ ... data/ ...
#      preprocessed/ ... featured/ ...
#
#  Run:   python app.py
#  Open:  http://localhost:5000
# ================================================================

from flask import Flask, render_template, jsonify, request, send_from_directory
import pandas as pd
import numpy as np
import joblib
import os
import sys
import time
import warnings
warnings.filterwarnings('ignore')

# Windows pipes stdout as cp1252 by default, which can't encode the ✓/✗/○
# markers the startup log below uses — crashes the whole app before it can
# bind to a port. Force utf-8 so startup logging never takes the app down.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

try:
    from flask_cors import CORS
    HAS_CORS = True
except ImportError:
    HAS_CORS = False

from flask import render_template_string, Response

# ----------------------------------------------------------------
# Embedded front-end (see dashboard_assets.py for why it exists).
# The dashboard runs from these strings whenever the matching file
# on disk is missing, so a deleted/quarantined template can never
# take the whole app down again.
# ----------------------------------------------------------------
try:
    from dashboard_assets import DASHBOARD_HTML, DASHBOARD_JS
    HAS_EMBEDDED = True
except Exception as _e:
    DASHBOARD_HTML = DASHBOARD_JS = None
    HAS_EMBEDDED = False
    # Optional fallback only — templates/dashboard.html is used normally.

app = Flask(__name__)
if HAS_CORS:
    CORS(app)

# ================================================================
# PATHS — every folder your notebooks write to
# ================================================================
APP_DIR = os.path.dirname(os.path.abspath(__file__))   # dashboard/ (templates, static)
BASE    = os.path.dirname(APP_DIR)                     # project root
MODELS  = os.path.join(BASE, 'models')
RESULTS = os.path.join(BASE, 'results')
CHARTS  = os.path.join(BASE, 'results', 'charts')
DATA    = os.path.join(BASE, 'data')
PREPROC = os.path.join(DATA, 'preprocessed')
FEATURED = os.path.join(DATA, 'featured')

# Files bigger than this are registered but NOT read at startup —
# they are read on first request and then cached in memory.
EAGER_CSV_LIMIT_MB = 40
EAGER_PKL_LIMIT_MB = 50

print("\n" + "=" * 68)
print("  FinShield AI — starting up")
print("=" * 68)


def mb(path):
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


# ================================================================
# 1. MODEL REGISTRY  — every .pkl / .pt in models/, featured/, preprocessed/
# ================================================================
MODEL_REGISTRY = {}   # short name -> dict(path, size_mb, loaded, obj, kind, error)


def register_models():
    folders = [('models', MODELS), ('featured', FEATURED), ('preprocessed', PREPROC)]
    for folder_name, folder in folders:
        if not os.path.isdir(folder):
            print(f"  ! folder not found: {folder_name}/")
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(('.pkl', '.pt')):
                continue
            path = os.path.join(folder, fn)
            key = fn[:-4] if folder_name == 'models' else f"{folder_name}/{fn[:-4]}"
            MODEL_REGISTRY[key] = dict(
                file=f"{folder_name}/{fn}", path=path, size_mb=round(mb(path), 2),
                loaded=False, obj=None, kind=None, error=None, eager=mb(path) <= EAGER_PKL_LIMIT_MB,
            )


def load_model(key):
    """Load (and cache) one registered model. Returns the object or None."""
    e = MODEL_REGISTRY.get(key)
    if e is None:
        return None
    if e['loaded']:
        return e['obj']
    if e['path'].lower().endswith('.pt'):
        # PyTorch weights — we record them but do not need torch to run the dashboard
        e.update(loaded=True, obj=None, kind='pytorch_state_dict (not loaded — torch not required)')
        return None
    try:
        t0 = time.time()
        obj = joblib.load(e['path'])
        e.update(loaded=True, obj=obj, kind=type(obj).__name__,
                 load_s=round(time.time() - t0, 2))
        return obj
    except Exception as ex:
        e.update(loaded=True, obj=None, error=str(ex)[:200])
        return None


register_models()
print(f"\n[Models]  {len(MODEL_REGISTRY)} model files found")
for k, e in MODEL_REGISTRY.items():
    if e['eager']:
        load_model(k)
        mark = '✓' if e['error'] is None else '✗'
        note = e['error'] or e['kind'] or ''
        print(f"  {mark} {e['file']:38} {e['size_mb']:>8.2f} MB   {note}")
    else:
        print(f"  ○ {e['file']:38} {e['size_mb']:>8.2f} MB   (large — loads on first use)")


def model_obj(key):
    e = MODEL_REGISTRY.get(key)
    if e and e['loaded']:
        return e['obj']
    return load_model(key)


# Convenience handles used across the API
model_xgb = model_obj('xgboost_model')      # 30-feature production model (SHAP / risk engine / live predict)
shap_exp  = model_obj('shap_explainer')
risk_cfg  = model_obj('risk_engine_config') or {}
best_name = model_obj('best_ml_model_name') or 'XGBoost (300 trees)'

XGB_FEATURES = []
if model_xgb is not None and hasattr(model_xgb, 'feature_names_in_'):
    XGB_FEATURES = [str(f) for f in model_xgb.feature_names_in_]
    print(f"  → live-prediction model expects {len(XGB_FEATURES)} features")

# Maps the display name in ml_comparison_results.csv to its saved .pkl
ML_MODEL_FILES = {
    'XGBoost (300 trees)':       'xgboost',
    'LightGBM (300 trees)':      'lightgbm',
    'Hist Gradient Boosting':    'gradient_boosting',
    'Random Forest (200 trees)': 'random_forest',
    'AdaBoost':                  'extra_trees',
    'Decision Tree (depth=5)':   'decision_tree',
    'Logistic Regression':       'logistic_regression',
}

# ================================================================
# 2. CSV REGISTRY — every .csv in results/ (+ the cleaned dataset)
# ================================================================
CSV_REGISTRY = {}   # key -> dict(path, size_mb, rows, loaded, df, eager)


def register_csvs():
    for folder_name, folder in [('results', RESULTS), ('data', DATA)]:
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith('.csv'):
                continue
            path = os.path.join(folder, fn)
            key = fn[:-4] if folder_name == 'results' else f"{folder_name}/{fn[:-4]}"
            CSV_REGISTRY[key] = dict(
                file=f"{folder_name}/{fn}", path=path, size_mb=round(mb(path), 2),
                loaded=False, df=None, rows=None, error=None,
                eager=mb(path) <= EAGER_CSV_LIMIT_MB,
            )


def load_csv(key, **kwargs):
    e = CSV_REGISTRY.get(key)
    if e is None:
        return pd.DataFrame()
    if e['loaded'] and e['df'] is not None:
        return e['df']
    try:
        df = pd.read_csv(e['path'], **kwargs)
        e.update(loaded=True, df=df, rows=len(df))
        return df
    except Exception as ex:
        e.update(loaded=True, df=pd.DataFrame(), error=str(ex)[:200])
        return pd.DataFrame()


register_csvs()
print(f"\n[Result CSVs]  {len(CSV_REGISTRY)} csv files found")
for k, e in CSV_REGISTRY.items():
    if e['eager']:
        load_csv(k)
        mark = '✓' if not e['error'] else '✗'
        print(f"  {mark} {e['file']:38} {e['size_mb']:>8.2f} MB   {e['rows'] or 0:,} rows")
    else:
        print(f"  ○ {e['file']:38} {e['size_mb']:>8.2f} MB   (large — reads on demand)")

# eager handles
ml_df          = load_csv('ml_comparison_results')
xgb_metrics_df = load_csv('xgb_metrics')
cnn_metrics_df = load_csv('cnn_metrics')
shap_imp_df    = load_csv('shap_feature_importance')
graph_comm_df  = load_csv('graph_community_analysis')
lstm_pred_df   = load_csv('lstm_predictions')
cnn_scores_df  = load_csv('cnn_scores')
alerts_df      = load_csv('alerts')

# ----------------------------------------------------------------
# TRANSACTION OVERVIEW INDEX — powers /api/transaction/<id> and
# /api/account/<id>/network. Built offline by precompute_transactions.py
# from data/cleaned_transactions.csv (real amounts, banks, currencies,
# preprocessing flags) + the Risk Engine's score/level, so no big CSV is
# read on a live request. Transaction ID = 1-based row number.
# Kept as plain numpy arrays; an account's transactions are found by
# binary search in arrays sorted by sender / receiver.
# ----------------------------------------------------------------
import json as _json
TXN_OVERVIEW_PARQUET = os.path.join(DATA, 'precomputed', 'transactions_overview.parquet')
ACCOUNT_FLAGS_JSON = os.path.join(DATA, 'precomputed', 'account_graph_flags.json')

TXN_OV = {}          # column name -> numpy array (row i = transaction ID i + 1)
TXN_BY = {}          # 'sender' / 'receiver' -> (row order, sorted account IDs)
try:
    if os.path.exists(TXN_OVERVIEW_PARQUET):
        _t = pd.read_parquet(TXN_OVERVIEW_PARQUET)
        TXN_OV = {c: _t[c].to_numpy() for c in _t.columns}
        TXN_OV['risk_level'] = _t['risk_level'].astype(str).to_numpy()
        del _t
        for _key in ('sender', 'receiver'):
            _order = np.argsort(TXN_OV[_key], kind='stable').astype(np.int32)
            TXN_BY[_key] = (_order, TXN_OV[_key][_order])
        print(f"  ✓ transaction overview index loaded  ({len(TXN_OV['sender']):,} rows)")
    else:
        print("  ○ transaction overview index not found — run precompute_transactions.py "
              "to enable the Transaction Overview and Linked Account Network cards")
except Exception as ex:
    TXN_OV, TXN_BY = {}, {}
    print(f"  ✗ failed to load transaction overview index: {ex}")

# account (str) -> [community_id or None, is_mule], from the Graph notebook
ACCOUNT_FLAGS, ACCOUNT_SUGGESTIONS = {}, []
try:
    if os.path.exists(ACCOUNT_FLAGS_JSON):
        with open(ACCOUNT_FLAGS_JSON) as f:
            _flags = _json.load(f)
        ACCOUNT_FLAGS = _flags.get('accounts', {})
        ACCOUNT_SUGGESTIONS = _flags.get('suggestions', [])
        print(f"  ✓ account community/mule tags loaded  ({len(ACCOUNT_FLAGS):,} accounts)")
except Exception as ex:
    print(f"  ✗ failed to load account community/mule tags: {ex}")

community_stats = {}
if not graph_comm_df.empty and 'graph_community_id' in graph_comm_df.columns:
    for _, r in graph_comm_df.iterrows():
        community_stats[int(r['graph_community_id'])] = dict(
            transaction_count=int(r.get('transaction_count', 0)),
            fraud_rate=round(float(r.get('fraud_rate', 0)), 6),
            suspicious=bool(r.get('suspicious_community', False)),
        )

# ================================================================
# 3. CHART REGISTRY — every .png, auto-assigned to a dashboard tab
# ================================================================
CHART_RULES = [
    ('cnn',      lambda n: n.startswith('cnn_')),
    ('lstm',     lambda n: n.startswith('lstm_') or n.startswith('ae_')),
    ('shap',     lambda n: n.startswith('shap_')),
    ('graph',    lambda n: n.startswith('graph_') or n in ('fan_pattern_analysis.png', 'circular_community.png')),
    ('risk',     lambda n: n.startswith('risk_')),
    ('mlmodels', lambda n: n in ('ml_model_comparison.png', 'all_confusion_matrices.png',
                                 'precision_recall_curves.png', 'feature_importance_comparison.png',
                                 'decision_tree_rules.png', 'confusion_roc.png',
                                 'feature_importance.png', 'pr_threshold.png')),
    ('cleaning', lambda n: n == 'class_balance_analysis.png'),
]

CHART_CAPTIONS = {
    'ml_model_comparison.png': 'Master comparison — 8-panel summary across all 7 models',
    'all_confusion_matrices.png': 'Confusion matrices — all 7 models',
    'precision_recall_curves.png': 'Precision–recall curves (AP annotated per model)',
    'feature_importance_comparison.png': 'Feature importance — Random Forest, Extra Trees, XGBoost',
    'decision_tree_rules.png': 'Decision Tree (depth 5) — visualised split rules',
    'confusion_roc.png': 'XGBoost confusion matrix + ROC curve',
    'feature_importance.png': 'XGBoost feature importance',
    'pr_threshold.png': 'Precision / recall vs decision threshold',
    'class_balance_analysis.png': 'Class balance — 979:1 imbalance',
    'cnn_performance.png': 'CNN-1D full performance breakdown',
    'cnn_training_progress.png': 'CNN-1D training loss & validation AUC (5 epochs)',
    'cnn_filter_analysis.png': 'What each convolutional filter learned',
    'lstm_training.png': 'LSTM training loss & validation AUC per epoch',
    'lstm_results.png': 'LSTM confusion matrix & score distribution',
    'lstm_vs_xgb.png': 'LSTM vs XGBoost — head-to-head metrics',
    'ae_performance.png': 'Autoencoder performance',
    'ae_training_progress.png': 'Autoencoder training progress',
    'graph_network.png': 'Transaction network — fraud (red), mule (orange), normal (blue)',
    'graph_metrics.png': 'PageRank, degree & centrality distributions',
    'fan_pattern_analysis.png': 'Fan-in / fan-out pattern analysis',
    'circular_community.png': 'Circular-flow detection & community structure',
    'shap_global_importance.png': 'SHAP global feature importance — top 20',
    'shap_beeswarm.png': 'SHAP beeswarm — every transaction, coloured by feature value',
    'shap_waterfall.png': 'SHAP waterfall — why the top 5 riskiest were flagged',
    'shap_dependence.png': 'SHAP dependence — top 4 features',
    'shap_bar_summary.png': 'SHAP clean summary bar chart',
    'shap_summary_bar_shap.png': 'SHAP built-in summary bar',
    'risk_engine_analysis.png': 'Risk engine analysis — 9-panel summary',
    'eda_charts/amount_distribution_comparison.png': 'Amount distribution — fraud vs normal (log scale)',
    'eda_charts/cross_bank_self_transfer_fraud.png': 'Cross-bank & self-transfer fraud rate',
    'eda_charts/feature_correlation_analysis.png': 'Feature correlation heatmap',
    'eda_charts/fraud_by_outlier_score.png': 'Fraud rate by combined outlier score',
    'eda_charts/fraud_by_payment_format.png': 'Fraud rate by payment format',
    'eda_charts/fraud_by_weekday_weekend.png': 'Weekday vs weekend, and by day of week',
    'eda_charts/fraud_time_pattern_by_hour.png': 'Full time-pattern breakdown by hour',
}

# Files that are on disk but are blank/corrupt exports — hidden from galleries
# and reported on /api/inventory so nothing is silently dropped.
BLANK_CHARTS = set()


def is_blank_png(path):
    """A matplotlib figure saved after plt.show() comes out as a blank white
    PNG. Those compress to almost nothing for their pixel count — detect that
    ratio so we can substitute a live chart instead of showing an empty box."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(24)
        if head[:8] != b'\x89PNG\r\n\x1a\n':
            return True
        w = int.from_bytes(head[16:20], 'big')
        h = int.from_bytes(head[20:24], 'big')
        if w * h == 0:
            return True
        return (size / (w * h)) < 0.006     # blank pages sit far below this
    except Exception:
        return False


CHART_REGISTRY = {}      # 'name.png' or 'eda_charts/name.png' -> dict


def register_charts():
    if not os.path.isdir(CHARTS):
        print("  ! charts/ folder not found")
        return
    entries = [(f, os.path.join(CHARTS, f)) for f in sorted(os.listdir(CHARTS))
               if f.lower().endswith('.png')]
    eda_dir = os.path.join(CHARTS, 'eda_charts')
    if os.path.isdir(eda_dir):
        entries += [(f'eda_charts/{f}', os.path.join(eda_dir, f))
                    for f in sorted(os.listdir(eda_dir)) if f.lower().endswith('.png')]

    for name, path in entries:
        base = os.path.basename(name)
        tab = 'eda' if name.startswith('eda_charts/') else 'other'
        if not name.startswith('eda_charts/'):
            for t, rule in CHART_RULES:
                if rule(base):
                    tab = t
                    break
        blank = is_blank_png(path)
        if blank:
            BLANK_CHARTS.add(name)
        CHART_REGISTRY[name] = dict(
            name=name, tab=tab, size_kb=round(os.path.getsize(path) / 1024, 1),
            caption=CHART_CAPTIONS.get(name, base.replace('_', ' ').replace('.png', '')),
            blank=blank,
        )


register_charts()
_ok = [c for c in CHART_REGISTRY.values() if not c['blank']]
print(f"\n[Charts]  {len(CHART_REGISTRY)} png files found — {len(_ok)} usable, "
      f"{len(BLANK_CHARTS)} blank/corrupt")
for n in sorted(BLANK_CHARTS):
    print(f"  ✗ blank export (a live chart is shown instead): {n}")
_by_tab = {}
for c in CHART_REGISTRY.values():
    if not c['blank']:
        _by_tab[c['tab']] = _by_tab.get(c['tab'], 0) + 1
print("  charts per tab:", ', '.join(f"{k}={v}" for k, v in sorted(_by_tab.items())))

# ================================================================
# SEQUENCE-MODEL METRICS  (LSTM + CNN)
# ----------------------------------------------------------------
# Both notebooks now save a score for EVERY sequence plus an
# `is_train` flag. Metrics must be computed on the TEST rows only —
# scoring over train+test together would flatter the model.
# Older files (before the notebook fixes) have no `is_train` column;
# those were test-only already, so we use every row and say so.
# ================================================================
def sequence_metrics(df, score_col, label_col='actual_label',
                     pred_col=None, threshold=0.5):
    if df is None or df.empty or score_col not in df.columns or label_col not in df.columns:
        return {}
    try:
        from sklearn.metrics import (roc_auc_score, average_precision_score,
                                     precision_score, recall_score, f1_score,
                                     confusion_matrix)
        total_rows = len(df)
        if 'is_train' in df.columns:
            sub = df[~df['is_train'].astype(bool)]
            scope = 'test sequences only (held-out accounts)'
        else:
            sub = df
            scope = 'all rows in file (no is_train column — older format)'
        if sub.empty or sub[label_col].nunique() < 2:
            return {}

        y = sub[label_col].astype(int)
        p = sub[score_col].astype(float)
        pred = (sub[pred_col].astype(int) if pred_col and pred_col in sub.columns
                else (p >= threshold).astype(int))

        cm = confusion_matrix(y, pred, labels=[0, 1])
        tn, fp, fn, tp = cm.ravel()
        return dict(
            auc_roc=round(float(roc_auc_score(y, p)), 4),
            auc_pr=round(float(average_precision_score(y, p)), 4),
            recall=round(float(recall_score(y, pred, zero_division=0)), 4),
            precision=round(float(precision_score(y, pred, zero_division=0)), 4),
            f1=round(float(f1_score(y, pred, zero_division=0)), 4),
            fraud_caught=int(tp), fraud_missed=int(fn),
            false_alarms=int(fp), true_negatives=int(tn),
            sequences=int(len(sub)), sequences_total=int(total_rows),
            scope=scope,
            mean_score_fraud=round(float(p[y == 1].mean()), 6) if (y == 1).any() else 0.0,
            mean_score_normal=round(float(p[y == 0].mean()), 6) if (y == 0).any() else 0.0,
        )
    except Exception as e:
        print(f"  ✗ metric computation failed for {score_col}: {e}")
        return {}


LSTM_METRICS = sequence_metrics(lstm_pred_df, 'lstm_anomaly_score',
                                pred_col='lstm_predicted')
if LSTM_METRICS:
    print(f"\n  → LSTM metrics from results/lstm_predictions.csv "
          f"({LSTM_METRICS['scope']}, AUC-ROC {LSTM_METRICS['auc_roc']})")
else:
    print("\n  ✗ LSTM metrics unavailable (results/lstm_predictions.csv missing or unusable)")

# CNN: after the notebook fix, cnn_scores.csv carries labels too, so the
# metrics can be recomputed here instead of trusting the saved summary row.
CNN_LIVE_METRICS = sequence_metrics(cnn_scores_df, 'cnn_fraud_score')
if CNN_LIVE_METRICS:
    print(f"  → CNN metrics recomputed from results/cnn_scores.csv "
          f"({CNN_LIVE_METRICS['scope']}, AUC-ROC {CNN_LIVE_METRICS['auc_roc']})")

print("=" * 68 + "\n")


# ================================================================
# HELPERS
# ================================================================
def sf(v, dec=4):
    try:
        x = float(v)
        return 0.0 if (x != x or abs(x) == float('inf')) else round(x, dec)
    except Exception:
        return 0.0


def si(v):
    try:
        if v != v:
            return 0
        return int(v)
    except Exception:
        return 0


DATASET_FACTS = dict(
    total_transactions=5_078_336, fraud_transactions=5_177, normal_transactions=5_073_159,
    fraud_rate_pct=0.1019, imbalance_ratio=979, unique_accounts=515_080,
    duplicates_removed=9, final_columns_cleaning=32,
    model_features=29, model_features_production=30,
    graph_nodes=515_080, graph_edges=1_015_736, graph_density=0.00000383,
    graph_cycles=812, graph_communities=3_351,
    test_rows=1_015_668, test_fraud=1_035,
)


def overview_payload():
    o = dict(DATASET_FACTS)
    o['best_model'] = str(best_name)
    o['best_model_auc'] = 0.9709
    if not ml_df.empty:
        try:
            row = ml_df.sort_values('AUC-ROC', ascending=False).iloc[0]
            o['best_model'] = str(row['Model'])
            o['best_model_auc'] = sf(row['AUC-ROC'])
        except Exception:
            pass
    cfg = risk_cfg or {}
    o['total_alerts'] = si(cfg.get('total_alerts', len(alerts_df)))
    o['fraud_captured'] = si(cfg.get('fraud_captured', 0))
    o['total_fraud_cfg'] = si(cfg.get('total_fraud', DATASET_FACTS['fraud_transactions']))
    o['capture_rate_pct'] = sf(cfg.get('capture_rate_pct', 0), 2)
    o['alert_rate_pct'] = sf(cfg.get('alert_rate_pct', 0), 2)
    o['auc_xgb_production'] = sf(cfg.get('auc_xgb', 0.911), 4)
    o['auc_ensemble'] = sf(cfg.get('auc_ensemble', 0.7039), 4)
    o['files'] = dict(
        models=len(MODEL_REGISTRY),
        models_loaded=sum(1 for e in MODEL_REGISTRY.values() if e['obj'] is not None),
        csvs=len(CSV_REGISTRY),
        charts=len(CHART_REGISTRY),
        charts_usable=len(CHART_REGISTRY) - len(BLANK_CHARTS),
    )
    return o


# ================================================================
# ERROR REPORTING
# ----------------------------------------------------------------
# Flask's default 500 page is the bare string "Internal Server Error",
# which tells you nothing. This prints the full traceback to the
# terminal in a boxed block AND shows it in the browser, so a failure
# is diagnosable without guesswork.
# ================================================================
import traceback


@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_any_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException) and e.code != 500:
        return e                      # let 404s etc. behave normally

    tb = traceback.format_exc()
    print("\n" + "!" * 68)
    print("  ERROR while handling:", request.method, request.path)
    print("!" * 68)
    print(tb)
    print("!" * 68 + "\n")

    if request.path.startswith('/api/'):
        return jsonify({'error': str(e), 'where': request.path,
                        'traceback': tb.split('\n')[-4:]}), 500

    return (f"""<html><body style="background:#0A1424;color:#E9F1FA;
        font-family:ui-monospace,Consolas,monospace;padding:28px;line-height:1.6">
        <h2 style="color:#e35555;margin-bottom:4px">FinShield AI — server error</h2>
        <p style="color:#93A9C4">while handling <b>{request.method} {request.path}</b></p>
        <p style="color:#93A9C4">The same traceback has been printed in your terminal.
        Copy it from there (or below) to diagnose.</p>
        <pre style="background:#050B16;border:1px solid #24395C;border-radius:8px;
        padding:16px;overflow:auto;white-space:pre-wrap;color:#9fe8ff">{tb}</pre>
        </body></html>""", 500)


@app.route('/api/selftest')
def api_selftest():
    """Calls every read-only endpoint in-process and reports which fail.
    Open http://localhost:5000/api/selftest to find a broken panel fast."""
    checks = ['health', 'inventory', 'overview', 'charts_list', 'ml_comparison',
              'xgb_metrics', 'cnn_metrics', 'lstm_metrics', 'model_health',
              'graph_communities', 'shap_importance', 'risk_overview',
              'risk_distribution', 'risk_diagnostics', 'class_balance',
              'fraud_by_hour', 'predict_meta', 'predict_thresholds',
              'cnn_score_distribution', 'alerts']
    out = {}
    with app.test_client() as c:
        for name in checks:
            try:
                r = c.get('/api/' + name)
                out[name] = ('OK' if r.status_code == 200
                             else f'HTTP {r.status_code}')
            except Exception as ex:
                out[name] = f'EXCEPTION: {type(ex).__name__}: {ex}'
    failing = {k: v for k, v in out.items() if v != 'OK'}
    print("\n[selftest]", "all endpoints OK" if not failing else f"FAILING: {failing}")
    return jsonify(dict(all_ok=not failing, failing=failing, results=out))


# ================================================================
# PAGE + STATIC
# ================================================================
def _page_html():
    """Prefer templates/dashboard.html so you can edit it; fall back to the
    copy embedded in dashboard_assets.py when the file is absent."""
    disk = os.path.join(APP_DIR, 'templates', 'dashboard.html')
    if os.path.exists(disk):
        try:
            with open(disk, encoding='utf-8') as f:
                return f.read(), 'templates/dashboard.html'
        except Exception as ex:
            print(f"  ! could not read {disk}: {ex}")
    if HAS_EMBEDDED:
        return DASHBOARD_HTML, 'embedded (dashboard_assets.py)'
    return None, None


@app.route('/')
def index():
    html, source = _page_html()
    if html is None:
        msg = ("Neither templates/dashboard.html nor dashboard_assets.py is "
               "available. Put dashboard_assets.py next to app.py.")
        print("\n" + "!" * 68 + "\n  " + msg + "\n" + "!" * 68 + "\n")
        return (f"<html><body style='background:#0A1424;color:#E9F1FA;"
                f"font-family:system-ui;padding:34px'><h2 style='color:#fab219'>"
                f"Front-end not found</h2><p>{msg}</p>"
                f"<p style='color:#93A9C4'>Expected in: <code>{BASE}</code></p>"
                f"<p><a style='color:#00D4FF' href='/api/selftest'>/api/selftest</a></p>"
                f"</body></html>", 503)

    # If the vendored Chart.js is missing, fall back to the CDN so charts
    # still render instead of silently failing.
    if not os.path.exists(os.path.join(APP_DIR, 'static', 'chart.umd.min.js')):
        html = html.replace('/static/chart.umd.min.js',
                            'https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js')
        print("  ! static/chart.umd.min.js missing — using the Chart.js CDN")

    resp = app.make_response(render_template_string(html, overview=overview_payload()))
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['X-Dashboard-Source'] = source
    return resp


@app.route('/static/dashboard.js')
def serve_dashboard_js():
    """Explicit route so the JS is served from the embedded copy if the
    file on disk has been removed."""
    disk = os.path.join(APP_DIR, 'static', 'dashboard.js')
    if os.path.exists(disk):
        return send_from_directory(os.path.join(APP_DIR, 'static'), 'dashboard.js')
    if HAS_EMBEDDED:
        return Response(DASHBOARD_JS, mimetype='application/javascript',
                        headers={'Cache-Control': 'no-store'})
    return Response('console.error("dashboard.js not found");',
                    mimetype='application/javascript', status=404)


@app.route('/charts/<path:filename>')
def serve_chart(filename):
    if filename.startswith('eda_charts/'):
        return send_from_directory(os.path.join(CHARTS, 'eda_charts'), filename[len('eda_charts/'):])
    return send_from_directory(CHARTS, filename)


# ================================================================
# API — INVENTORY / HEALTH / OVERVIEW
# ================================================================
@app.route('/api/health')
def api_health():
    return jsonify(dict(
        status='running',
        models_found=len(MODEL_REGISTRY),
        models_loaded=sum(1 for e in MODEL_REGISTRY.values() if e['obj'] is not None),
        csvs_found=len(CSV_REGISTRY),
        charts_found=len(CHART_REGISTRY),
        charts_blank=len(BLANK_CHARTS),
        xgboost_loaded=model_xgb is not None,
        shap_loaded=shap_exp is not None,
        alerts_rows=len(alerts_df),
    ))


@app.route('/api/inventory')
def api_inventory():
    """Every file the dashboard discovered — models, CSVs and charts."""
    return jsonify(dict(
        models=[dict(name=k, file=e['file'], size_mb=e['size_mb'],
                     loaded=e['obj'] is not None, kind=e['kind'], error=e['error'])
                for k, e in MODEL_REGISTRY.items()],
        csvs=[dict(name=k, file=e['file'], size_mb=e['size_mb'],
                   loaded=e['loaded'], rows=e['rows'], error=e['error'])
              for k, e in CSV_REGISTRY.items()],
        charts=[dict(name=c['name'], tab=c['tab'], size_kb=c['size_kb'],
                     caption=c['caption'], blank=c['blank'])
                for c in CHART_REGISTRY.values()],
    ))


@app.route('/api/overview')
def api_overview():
    return jsonify(overview_payload())


@app.route('/api/charts_list')
def api_charts_list():
    """Charts for one tab (?tab=cnn) or all of them, grouped."""
    tab = request.args.get('tab')
    items = [c for c in CHART_REGISTRY.values() if not c['blank']]
    if tab:
        items = [c for c in items if c['tab'] == tab]
    return jsonify({'charts': [dict(name=c['name'], caption=c['caption'], tab=c['tab'])
                               for c in items]})


# ================================================================
# API — NOTEBOOK 01/02 : CLEANING + EDA (live computed)
# ================================================================
_HOUR_CACHE = {}


@app.route('/api/fraud_by_hour')
def api_fraud_by_hour():
    """Fraud rate for each hour 0-23, computed from the cleaned dataset.

    The notebook's own 'Fraud by hour of day.png' saved as a blank image, so
    this recomputes it from data/cleaned_transactions.csv (5.08M rows, read
    once with only the two needed columns, then cached in memory)."""
    if _HOUR_CACHE:
        return jsonify(_HOUR_CACHE)

    for key in ('data/cleaned_transactions', 'cleaned_transactions'):
        e = CSV_REGISTRY.get(key)
        if not e:
            continue
        try:
            print(f"  … computing fraud-by-hour from {e['file']} (first request only)")
            df = pd.read_csv(e['path'], usecols=['Hour', 'Is Laundering'])
            g = df.groupby('Hour')['Is Laundering'].agg(['count', 'sum'])
            hours = list(range(24))
            out = dict(
                source=e['file'],
                hours=hours,
                totals=[si(g['count'].get(h, 0)) for h in hours],
                frauds=[si(g['sum'].get(h, 0)) for h in hours],
                fraud_rate=[sf(g['sum'].get(h, 0) / g['count'].get(h, 1) * 100, 6) for h in hours],
            )
            _HOUR_CACHE.update(out)
            print("  ✓ fraud-by-hour cached")
            return jsonify(out)
        except Exception as ex:
            print(f"  ✗ fraud-by-hour failed on {e['file']}: {ex}")

    return jsonify({'error': 'data/cleaned_transactions.csv not found', 'hours': []})


@app.route('/api/class_balance')
def api_class_balance():
    """Class-imbalance figures. The notebook's 'Class Balance Analysis.png'
    also saved blank, so the dashboard draws this live instead."""
    return jsonify(dict(
        normal=DATASET_FACTS['normal_transactions'],
        fraud=DATASET_FACTS['fraud_transactions'],
        total=DATASET_FACTS['total_transactions'],
        fraud_pct=DATASET_FACTS['fraud_rate_pct'],
        ratio=DATASET_FACTS['imbalance_ratio'],
        train_normal=4_058_526, train_fraud=4_142,
        smote_normal=4_058_526, smote_fraud=1_217_557,
        test_normal=DATASET_FACTS['test_rows'] - DATASET_FACTS['test_fraud'],
        test_fraud=DATASET_FACTS['test_fraud'],
    ))


# ================================================================
# API — NOTEBOOK 05 : ML MODEL COMPARISON
# ================================================================
def _ml_rows():
    if ml_df.empty:
        return []
    out = []
    for _, r in ml_df.iterrows():
        name = str(r.get('Model', ''))
        caught, missed = si(r.get('Fraud Caught', 0)), si(r.get('Fraud Missed', 0))
        alarms = si(r.get('False Alarms', 0))
        total_fraud = caught + missed
        total_rows = DATASET_FACTS['test_rows']
        out.append(dict(
            name=name,
            key=ML_MODEL_FILES.get(name, ''),
            auc_roc=sf(r.get('AUC-ROC', 0)), auc_pr=sf(r.get('AUC-PR', 0)),
            recall=sf(r.get('Recall', 0)), precision=sf(r.get('Precision', 0)),
            f1=sf(r.get('F1 Score', 0)), mcc=sf(r.get('MCC', 0)),
            fraud_caught=caught, fraud_missed=missed, false_alarms=alarms,
            train_time_s=sf(r.get('Train Time (s)', 0), 1),
            true_negatives=max(0, total_rows - total_fraud - alarms),
            total_fraud=total_fraud, test_rows=total_rows,
        ))
    return out


@app.route('/api/ml_comparison')
def api_ml_comparison():
    return jsonify({'models': _ml_rows()})


@app.route('/api/model_detail')
def api_model_detail():
    """Everything known about ONE of the 7 compared models, including the
    saved .pkl it came from and that file's real hyper-parameters."""
    name = request.args.get('name', '')
    rows = _ml_rows()
    row = next((r for r in rows if r['name'] == name), None)
    if row is None:
        return jsonify({'error': f'unknown model: {name}'}), 404

    detail = dict(row)
    key = row['key']
    entry = MODEL_REGISTRY.get(key)
    if entry:
        obj = model_obj(key)   # loads random_forest.pkl on demand
        detail['file'] = entry['file']
        detail['size_mb'] = entry['size_mb']
        detail['loaded'] = obj is not None
        detail['class'] = type(obj).__name__ if obj is not None else None
        detail['n_features'] = int(getattr(obj, 'n_features_in_', 0)) if obj is not None else 0
        if obj is not None:
            try:
                params = obj.get_params()
                keep = ('n_estimators', 'max_depth', 'learning_rate', 'max_iter',
                        'min_samples_leaf', 'C', 'penalty', 'solver', 'num_leaves',
                        'scale_pos_weight', 'class_weight', 'random_state', 'subsample',
                        'colsample_bytree')
                detail['params'] = {k: str(v) for k, v in params.items()
                                    if k in keep and v is not None}
            except Exception:
                detail['params'] = {}
            # feature importance if the model exposes it
            try:
                imp = getattr(obj, 'feature_importances_', None)
                names = [str(f) for f in getattr(obj, 'feature_names_in_', [])]
                if imp is not None and names:
                    pairs = sorted(zip(names, [float(x) for x in imp]),
                                   key=lambda t: t[1], reverse=True)[:15]
                    detail['top_features'] = [dict(feature=f, importance=round(v, 5))
                                              for f, v in pairs]
            except Exception:
                pass
    else:
        detail['file'] = None
        detail['loaded'] = False
    return jsonify(detail)


# ================================================================
# API — DEEP LEARNING + GRAPH + SHAP
# ================================================================
@app.route('/api/xgb_metrics')
def api_xgb_metrics():
    if xgb_metrics_df.empty:
        return jsonify({})
    r = xgb_metrics_df.iloc[0].to_dict()
    return jsonify({k: (sf(v) if isinstance(v, (int, float, np.floating)) else str(v))
                    for k, v in r.items()})


@app.route('/api/cnn_metrics')
def api_cnn_metrics():
    """Saved summary row, overlaid with metrics recomputed live from
    cnn_scores.csv (test rows only) when that file carries labels."""
    out = {}
    if not cnn_metrics_df.empty:
        r = cnn_metrics_df.iloc[0].to_dict()
        out = {k: (sf(v) if isinstance(v, (int, float, np.floating)) else str(v))
               for k, v in r.items()}
    cfg = model_obj('cnn_config') or {}
    if isinstance(cfg, dict):
        out['input_channels'] = si(cfg.get('input_channels', 0))
        out['dropout_rate'] = sf(cfg.get('dropout_rate', 0), 2)
        if cfg.get('seq_len'):
            out['sequence_len'] = si(cfg.get('seq_len'))
        feats = cfg.get('features') or []
        out['n_features'] = len(feats) if hasattr(feats, '__len__') else 0
    if CNN_LIVE_METRICS:
        out.update(CNN_LIVE_METRICS)
        out['recomputed'] = True
    return jsonify(out)


@app.route('/api/cnn_score_distribution')
def api_cnn_score_dist():
    if cnn_scores_df.empty or 'cnn_fraud_score' not in cnn_scores_df.columns:
        return jsonify({'bins': [], 'counts': []})
    scores = cnn_scores_df['cnn_fraud_score'].values
    counts, edges = np.histogram(scores, bins=np.linspace(0, max(0.01, scores.max()), 25))
    return jsonify({'bins': edges[:-1].round(4).tolist(), 'counts': counts.tolist()})


@app.route('/api/lstm_metrics')
def api_lstm_metrics():
    out = dict(LSTM_METRICS)
    out['seq_len'] = si(model_obj('lstm_seq_len') or 10)
    feats = model_obj('lstm_features') or []
    out['n_features'] = len(feats) if hasattr(feats, '__len__') else 0
    return jsonify(out)


@app.route('/api/model_health')
def api_model_health():
    """One verdict per sequence model, derived from the metrics rather than
    written into the page. Keeps the dashboard honest after a re-run: if a
    model improves, the wording improves with it."""
    def verdict(m, name):
        if not m or 'auc_roc' not in m:
            return dict(name=name, state='missing',
                        headline=f'{name} results not found',
                        detail='Re-run the notebook to generate its score file.')
        auc = float(m['auc_roc'])
        rec, prec = float(m.get('recall', 0)), float(m.get('precision', 0))
        collapsed = rec >= 0.99 and prec < 0.05
        if collapsed:
            state, head = 'collapsed', f'{name} collapsed to predicting fraud for everything'
            detail = ('Recall is ~100% only because every sequence is being flagged; '
                      'precision near zero confirms it. This is the signature of a '
                      'saturated output layer, not a working detector.')
        elif auc < 0.55:
            state, head = 'random', f'{name} is near-random (AUC-ROC {auc:.4f})'
            detail = ('An AUC of 0.50 is a coin flip. With only ~0.1% fraud there may be '
                      'too few positive sequences for the model to learn a general pattern.')
        elif auc < 0.70:
            state, head = 'weak', f'{name} is weak but above random (AUC-ROC {auc:.4f})'
            detail = 'It has found some signal, but not enough to outrank the tabular model.'
        else:
            state, head = 'good', f'{name} is performing well (AUC-ROC {auc:.4f})'
            detail = 'This model separates fraud from normal and is worth ensembling.'
        return dict(name=name, state=state, headline=head, detail=detail,
                    auc_roc=auc, recall=rec, precision=prec,
                    scope=m.get('scope', ''), sequences=si(m.get('sequences', 0)))

    cnn_m = dict(CNN_LIVE_METRICS)
    if not cnn_m and not cnn_metrics_df.empty:
        cnn_m = {k: sf(v) for k, v in cnn_metrics_df.iloc[0].to_dict().items()
                 if isinstance(v, (int, float, np.floating))}
    return jsonify(dict(cnn=verdict(cnn_m, 'CNN-1D'),
                        lstm=verdict(LSTM_METRICS, 'LSTM')))


@app.route('/api/graph_communities')
def api_graph_communities():
    if graph_comm_df.empty:
        return jsonify({'communities': []})
    df2 = graph_comm_df.sort_values('transaction_count', ascending=False).head(25)
    return jsonify({'communities': [dict(
        community_id=si(r.get('graph_community_id', -1)),
        transaction_count=si(r.get('transaction_count', 0)),
        fraud_rate=sf(r.get('fraud_rate', 0) * 100, 4),
        suspicious=si(r.get('suspicious_community', 0)),
    ) for _, r in df2.iterrows()]})


@app.route('/api/shap_importance')
def api_shap_importance():
    if shap_imp_df.empty:
        return jsonify({'features': [], 'values': []})
    top = shap_imp_df.head(int(request.args.get('n', 20)))
    return jsonify(dict(features=top['feature'].tolist(),
                        values=top['mean_shap'].round(5).tolist()))


# ================================================================
# API — NOTEBOOK 10 : RISK SCORING ENGINE
# ================================================================
@app.route('/api/risk_overview')
def api_risk_overview():
    cfg = risk_cfg or {}
    weights = cfg.get('weights', {})
    return jsonify(dict(
        weights={k.replace('_score', ''): sf(v, 4) for k, v in weights.items() if v and v > 0},
        thresholds={k: sf(v, 2) for k, v in cfg.get('thresholds', {}).items()},
        total_transactions=si(cfg.get('total_transactions', DATASET_FACTS['total_transactions'])),
        total_alerts=si(cfg.get('total_alerts', len(alerts_df))),
        fraud_captured=si(cfg.get('fraud_captured', 0)),
        total_fraud=si(cfg.get('total_fraud', DATASET_FACTS['fraud_transactions'])),
        capture_rate_pct=sf(cfg.get('capture_rate_pct', 0), 2),
        alert_rate_pct=sf(cfg.get('alert_rate_pct', 0), 2),
        auc_xgb=sf(cfg.get('auc_xgb', 0.911), 4),
        auc_ensemble=sf(cfg.get('auc_ensemble', 0.7039), 4),
        # present once the corrected Risk_Scoring_Engine notebook has run
        score_diagnostics=cfg.get('score_diagnostics', {}),
        admission_gates=cfg.get('admission_gates', {}),
        scaling=str(cfg.get('scaling', 'min_max (old notebook)')),
    ))


_RISK_DIST_CACHE = {}


@app.route('/api/risk_distribution')
def api_risk_distribution():
    """Risk-level breakdown from the full 5M-row final_risk_scores.csv
    (~435 MB, read once then cached). Falls back to alerts.csv + the
    calibrated config if that file isn't present."""
    if _RISK_DIST_CACHE:
        return jsonify(_RISK_DIST_CACHE)

    cfg = risk_cfg or {}
    e = CSV_REGISTRY.get('final_risk_scores')
    if e and os.path.exists(e['path']):
        try:
            print("  … reading final_risk_scores.csv (first request only, please wait)")
            df = pd.read_csv(e['path'], usecols=['risk_level', 'Is Laundering'])
            counts, rates = {}, {}
            for lvl in ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']:
                sub = df[df['risk_level'] == lvl]
                counts[lvl] = si(len(sub))
                rates[lvl] = sf(sub['Is Laundering'].mean() * 100, 4) if len(sub) else 0.0
            out = dict(source='final_risk_scores.csv', level_counts=counts, fraud_by_level=rates)
            _RISK_DIST_CACHE.update(out)
            print("  ✓ risk distribution cached")
            return jsonify(out)
        except Exception as ex:
            print('  ✗ risk_distribution fallback:', ex)

    total = si(cfg.get('total_transactions', DATASET_FACTS['total_transactions']))
    if not alerts_df.empty and 'risk_level' in alerts_df.columns:
        high = si((alerts_df['risk_level'] == 'HIGH').sum())
        crit = si((alerts_df['risk_level'] == 'CRITICAL').sum())
    else:
        n = si(cfg.get('total_alerts', 0))
        high, crit = int(n * .8), int(n * .2)
    medium = int(total * 0.0746)
    counts = dict(LOW=max(0, total - high - crit - medium), MEDIUM=medium, HIGH=high, CRITICAL=crit)
    rates = {'LOW': 0.0854, 'MEDIUM': 0.2844}
    if not alerts_df.empty and 'Is Laundering' in alerts_df.columns:
        for lvl in ('HIGH', 'CRITICAL'):
            sub = alerts_df[alerts_df['risk_level'] == lvl]
            rates[lvl] = sf(sub['Is Laundering'].mean() * 100, 4) if len(sub) else 0.0
    out = dict(source='config+alerts (approx)', level_counts=counts, fraud_by_level=rates)
    _RISK_DIST_CACHE.update(out)
    return jsonify(out)


_RISK_DIAG_CACHE = {}


@app.route('/api/risk_diagnostics')
def api_risk_diagnostics():
    """Is the ensemble actually helping?

    Ranks all 5.08M transactions by the ensemble score and by the raw XGBoost
    score, and compares how much fraud each ranking captures at the SAME alert
    budget. If XGBoost alone beats the blend, the ensemble is destroying signal
    rather than adding it — which is exactly what the CNN's near-random
    contribution does here."""
    if _RISK_DIAG_CACHE:
        return jsonify(_RISK_DIAG_CACHE)

    e = CSV_REGISTRY.get('final_risk_scores')
    if not e or not os.path.exists(e['path']):
        return jsonify({'available': False,
                        'reason': 'results/final_risk_scores.csv not found'})
    try:
        print("  … running risk-engine diagnostics on final_risk_scores.csv (first request only)")
        df = pd.read_csv(e['path'], usecols=['Is Laundering', 'xgb_score', 'risk_score_100', 'risk_level'])
        y = df['Is Laundering'].values
        xgb = df['xgb_score'].values
        ens = df['risk_score_100'].values
        n, total_fraud = len(df), int(y.sum())

        alert_mask = df['risk_level'].isin(['HIGH', 'CRITICAL']).values
        cur_alerts = int(alert_mask.sum())
        cur_caught = int(y[alert_mask].sum())
        cur_rate = cur_alerts / n * 100

        def capture_at(scores, budget_pct):
            k = max(1, int(n * budget_pct / 100))
            idx = np.argpartition(-scores, k - 1)[:k]
            return int(y[idx].sum()), k

        sweep = []
        for pct in (cur_rate, 5.0, 10.0):
            x_caught, k = capture_at(xgb, pct)
            e_caught, _ = capture_at(ens, pct)
            sweep.append(dict(
                alert_pct=round(pct, 2), alerts=k,
                xgb_caught=x_caught, xgb_capture_pct=round(x_caught / total_fraud * 100, 2),
                ensemble_caught=e_caught, ensemble_capture_pct=round(e_caught / total_fraud * 100, 2),
            ))

        out = dict(
            available=True, rows=n, total_fraud=total_fraud,
            current=dict(alerts=cur_alerts, alert_pct=round(cur_rate, 2),
                         caught=cur_caught, capture_pct=round(cur_caught / total_fraud * 100, 2)),
            sweep=sweep,
            uplift=round(sweep[0]['xgb_capture_pct'] / max(0.01, sweep[0]['ensemble_capture_pct']), 2),
            weights=(risk_cfg or {}).get('weights', {}),
        )
        _RISK_DIAG_CACHE.update(out)
        print(f"  ✓ diagnostics: ensemble {out['current']['capture_pct']}% vs "
              f"XGBoost-alone {sweep[0]['xgb_capture_pct']}% at the same alert budget")
        return jsonify(out)
    except Exception as ex:
        print('  ✗ risk diagnostics failed:', ex)
        return jsonify({'available': False, 'reason': str(ex)[:200]})


@app.route('/api/alerts')
def api_alerts():
    if alerts_df.empty:
        return jsonify({'alerts': [], 'total': 0, 'pages': 0, 'page': 1})
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(100, int(request.args.get('per_page', 20)))
    level = request.args.get('level', '')
    min_score = sf(request.args.get('min_score', 0))

    df2 = alerts_df
    if level in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL') and 'risk_level' in df2.columns:
        df2 = df2[df2['risk_level'] == level]
    if min_score > 0 and 'risk_score_100' in df2.columns:
        df2 = df2[df2['risk_score_100'] >= min_score]
    if 'risk_score_100' in df2.columns:
        df2 = df2.sort_values('risk_score_100', ascending=False)

    total = len(df2)

    # Composition of the WHOLE queue (before filtering) so the UI can show
    # what the queue is actually made of. alerts.csv only ever contains
    # HIGH + CRITICAL, because that is what "alert" means in the risk engine.
    level_counts = {}
    if 'risk_level' in alerts_df.columns:
        level_counts = {k: int(v) for k, v in
                        alerts_df['risk_level'].value_counts().items()}

    # Optional representative sample instead of "highest score first", so the
    # first page isn't 1,269 pages of CRITICAL before a HIGH appears.
    if request.args.get('sample') == '1' and total > 0:
        df2 = df2.sample(n=min(total, per_page * 50), random_state=42)

    chunk = df2.iloc[(page - 1) * per_page: page * per_page]
    rows = [dict(
        alert_id=str(r.get('alert_id', '')), risk_level=str(r.get('risk_level', '?')),
        risk_score=round(sf(r.get('risk_score_100', 0)), 2),
        xgb_score=round(sf(r.get('xgb_score', 0)), 4),
        cnn_score=round(sf(r.get('cnn_score', 0)), 4),
        composite_score=round(sf(r.get('composite_score', 0)), 4),
        is_fraud=si(r.get('Is Laundering', 0)),
        status=str(r.get('alert_status', 'PENDING_REVIEW')),
    ) for _, r in chunk.iterrows()]
    return jsonify(dict(alerts=rows, total=total, page=page,
                        pages=max(1, (total + per_page - 1) // per_page),
                        level_counts=level_counts,
                        queue_total=int(len(alerts_df))))


# ================================================================
# API — TRANSACTION OVERVIEW (search by Transaction ID)
# Sender/receiver details, real amounts and the preprocessing flags for
# one transaction, from the index built by precompute_transactions.py.
# risk_score/risk_level come from the original batch Risk Engine run,
# not the live-prediction path.
# ================================================================
CURRENCY_CODES = {'Australian Dollar': 'AUD', 'Bitcoin': 'BTC', 'Brazil Real': 'BRL',
                  'Canadian Dollar': 'CAD', 'Euro': 'EUR', 'Mexican Peso': 'MXN', 'Ruble': 'RUB',
                  'Rupee': 'INR', 'Saudi Riyal': 'SAR', 'Shekel': 'ILS', 'Swiss Franc': 'CHF',
                  'UK Pound': 'GBP', 'US Dollar': 'USD', 'Yen': 'JPY', 'Yuan': 'CNY'}

TXN_FLAGS = [   # (column, label, what it means) — engineered in the preprocessing notebook
    ('self_transfer', 'Self-transfer', 'Sender and receiver are the same account'),
    ('cross_bank', 'Cross-bank transfer', 'Money moves between two different banks'),
    ('currency_mismatch', 'Currency mismatch', 'Paid in one currency, received in another'),
]


def _currency(code):
    # Currencies were label-encoded alphabetically, which is CURRENCIES' order.
    name = CURRENCIES[code] if 0 <= code < len(CURRENCIES) else f'currency #{code}'
    return name, CURRENCY_CODES.get(name, name)


def _need_overview_index():
    if not TXN_OV:
        return jsonify({'error': 'Transaction overview index not built yet. '
                                  'Run: python precompute_transactions.py'}), 503
    return None


@app.route('/api/transaction/<txn_id>')
def api_transaction(txn_id):
    err = _need_overview_index()
    if err:
        return err

    digits = ''.join(ch for ch in txn_id if ch.isdigit())
    if not digits:
        return jsonify({'error': f'"{txn_id}" is not a valid transaction ID.'}), 400

    idx, n = int(digits), len(TXN_OV['sender'])
    if not 1 <= idx <= n:
        return jsonify({'error': f'No transaction with ID {idx}. Valid range is 1–{n:,}.'}), 404

    i = idx - 1
    pay_name, pay_code = _currency(int(TXN_OV['pay_cur'][i]))
    recv_name, recv_code = _currency(int(TXN_OV['recv_cur'][i]))
    return jsonify(dict(
        transaction_id=idx,
        sender=dict(account=int(TXN_OV['sender'][i]), bank=int(TXN_OV['sender_bank'][i]),
                    amount=round(float(TXN_OV['amount_paid'][i]), 2),
                    currency=pay_name, currency_code=pay_code),
        receiver=dict(account=int(TXN_OV['receiver'][i]), bank=int(TXN_OV['receiver_bank'][i]),
                      amount=round(float(TXN_OV['amount_received'][i]), 2),
                      currency=recv_name, currency_code=recv_code),
        flags=[dict(key=k, label=label, detail=detail, on=bool(TXN_OV[k][i]))
               for k, label, detail in TXN_FLAGS],
        is_laundering=int(TXN_OV['label'][i]),
        risk_score=round(float(TXN_OV['risk_score'][i]), 2),
        risk_level=str(TXN_OV['risk_level'][i]),
    ))


# ================================================================
# API — LINKED ACCOUNT NETWORK VIEW
# One account's direct counterparties, built from its real transactions
# (so every edge carries the real amount moved, per direction), tagged
# with the Graph notebook's community + mule flags. Click-to-recenter on
# the frontend calls this again with the clicked account, which is how an
# analyst walks a money trail hop by hop without rendering all 515K
# accounts at once.
# ================================================================
def _account_rows(account, key):
    order, sorted_ids = TXN_BY[key]
    lo = np.searchsorted(sorted_ids, account, 'left')
    hi = np.searchsorted(sorted_ids, account, 'right')
    return order[lo:hi]


def _account_summary(account):
    community, mule = ACCOUNT_FLAGS.get(str(account), [None, 0])
    stats = community_stats.get(community, {}) if community is not None else {}
    return dict(account_id=str(account), community_id=community,
                community_fraud_rate=stats.get('fraud_rate'),
                community_suspicious=bool(stats.get('suspicious', False)),
                mule=bool(mule))


@app.route('/api/account/<account_id>/network')
def api_account_network(account_id):
    err = _need_overview_index()
    if err:
        return err

    raw = str(account_id).strip()
    if not raw.isdigit():
        return jsonify({'error': f'"{raw}" is not a valid account ID — account IDs are numbers, like 942.'}), 400
    acct = int(raw)
    out_rows = _account_rows(acct, 'sender')
    in_rows = _account_rows(acct, 'receiver')
    if not len(out_rows) and not len(in_rows):
        return jsonify({'error': f'Account {acct} has no transactions.'}), 404

    limit = max(1, min(50, int(sf(request.args.get('limit', 10), 0)) or 10))
    t = TXN_OV
    df = pd.DataFrame(dict(
        cp=np.concatenate([t['receiver'][out_rows], t['sender'][in_rows]]),
        direction=np.concatenate([np.zeros(len(out_rows), np.int8), np.ones(len(in_rows), np.int8)]),
        amount=np.concatenate([t['amount_paid'][out_rows], t['amount_received'][in_rows]]),
        cur=np.concatenate([t['pay_cur'][out_rows], t['recv_cur'][in_rows]]),
        label=np.concatenate([t['label'][out_rows], t['label'][in_rows]]),
    ))
    self_transfers = int((t['receiver'][out_rows] == acct).sum())
    df = df[df['cp'] != acct]

    neighbors = []
    edges = df.groupby(['cp', 'direction']).agg(txn_count=('amount', 'size'), fraud_count=('label', 'sum'))
    top = edges.sort_values('txn_count', ascending=False, kind='stable').head(limit)
    if len(top):
        wanted = df.set_index(['cp', 'direction']).loc[top.index].reset_index()
        per_cur = wanted.groupby(['cp', 'direction', 'cur'])['amount'].sum()
        for (cp, d), e in top.iterrows():
            totals = per_cur.loc[(cp, d)].sort_values(ascending=False)
            name, code = _currency(int(totals.index[0]))
            neighbors.append(dict(
                _account_summary(int(cp)), direction='out' if d == 0 else 'in',
                txn_count=int(e['txn_count']), fraud_count=int(e['fraud_count']),
                amount=round(float(totals.iloc[0]), 2), currency=name, currency_code=code,
                other_currencies=int(len(totals) - 1)))

    return jsonify(dict(
        center=_account_summary(acct),
        sent=int(len(out_rows)), received=int(len(in_rows)), self_transfers=self_transfers,
        out_degree=int(df.loc[df['direction'] == 0, 'cp'].nunique()),
        in_degree=int(df.loc[df['direction'] == 1, 'cp'].nunique()),
        total_neighbors=int(len(edges)),
        neighbors=neighbors,
        community_map_available=bool(ACCOUNT_FLAGS),
    ))


@app.route('/api/account/suggestions')
def api_account_suggestions():
    out = []
    for s in ACCOUNT_SUGGESTIONS:
        stats = community_stats.get(s.get('community'), {})
        out.append(dict(s, community_fraud_rate=stats.get('fraud_rate')))
    return jsonify(dict(suggestions=out))


# ================================================================
# SCORE CALIBRATION
# ----------------------------------------------------------------
# The risk engine's tiers were calibrated as PERCENTILES of the score
# distribution across all 5.08M transactions:
#     CRITICAL = top 0.5%   HIGH = top 2.5%   MEDIUM = top 10%
# ...but those cut-offs (27.00 / 23.66 / 10.13) live on the *ensemble*
# risk_score_100 scale, NOT on a raw XGBoost probability. Comparing a
# raw probability x100 against them is a category error: the model's
# 99.5th-percentile probability is only 0.049, so any transaction
# scoring above 0.27 would be labelled CRITICAL — which is why the
# live console previously called almost everything CRITICAL.
#
# The fix: convert the live probability into its PERCENTILE within the
# real distribution of XGBoost scores, then apply the same top-0.5 /
# 2.5 / 10% tiers the engine was designed around.
# ================================================================

# Measured on results/final_risk_scores.csv (all 5,078,336 transactions).
# Used only when neither score file is available.
FALLBACK_QUANTILES = [
    (0, 0.00017569), (10, 0.00061139), (20, 0.00073623), (30, 0.00099970),
    (40, 0.00114359), (50, 0.00164027), (60, 0.00217988), (70, 0.00289337),
    (75, 0.00397122), (80, 0.00536934), (85, 0.00693258), (90, 0.01034463),
    (92.5, 0.01097669), (95, 0.01225367), (96, 0.01391244), (97, 0.01988611),
    (97.5, 0.02263398), (98, 0.02567506), (98.5, 0.02827547), (99, 0.03749848),
    (99.25, 0.04202636), (99.5, 0.04939311), (99.75, 0.06275409),
    (99.9, 0.08851149), (99.99, 0.35014870), (100, 0.80006325),
]

# tier -> percentile cut-off (matches the notebook's calibration intent)
TIER_PERCENTILES = {'CRITICAL': 99.5, 'HIGH': 97.5, 'MEDIUM': 90.0}

_CAL = {}   # {'pcts': np.array, 'vals': np.array, 'source': str, 'thresholds': {...}}


def _build_calibration():
    """Percentile grid of XGBoost scores. Prefers the small single-column
    file; falls back to the big one; finally to the measured constants."""
    if _CAL:
        return _CAL

    for key, col in (('xgb_scores_all', 'xgb_fraud_score'),
                     ('final_risk_scores', 'xgb_score')):
        e = CSV_REGISTRY.get(key)
        if not e or not os.path.exists(e['path']):
            continue
        try:
            print(f"  … calibrating live-prediction scale from {e['file']} (first request only)")
            x = pd.read_csv(e['path'], usecols=[col])[col].values
            pcts = np.linspace(0, 100, 1001)
            vals = np.quantile(x, pcts / 100.0)
            _CAL.update(pcts=pcts, vals=vals, source=e['file'], n=len(x))
            break
        except Exception as ex:
            print(f"  ✗ calibration failed on {e['file']}: {ex}")

    if not _CAL:
        pcts = np.array([q for q, _ in FALLBACK_QUANTILES], dtype=float)
        vals = np.array([v for _, v in FALLBACK_QUANTILES], dtype=float)
        _CAL.update(pcts=pcts, vals=vals,
                    source='built-in constants (measured on the full 5.08M dataset)', n=5_078_336)

    _CAL['thresholds'] = {t: float(np.interp(q, _CAL['pcts'], _CAL['vals']))
                          for t, q in TIER_PERCENTILES.items()}
    print(f"  ✓ live-score calibration ready — CRITICAL ≥ {_CAL['thresholds']['CRITICAL']:.5f} "
          f"(top 0.5% of XGBoost scores)")
    return _CAL


def score_to_percentile(prob):
    c = _build_calibration()
    return float(np.clip(np.interp(prob, c['vals'], c['pcts']), 0.0, 100.0))


def level_from_prob(prob):
    c = _build_calibration()
    t = c['thresholds']
    if prob >= t['CRITICAL']:
        return 'CRITICAL'
    if prob >= t['HIGH']:
        return 'HIGH'
    if prob >= t['MEDIUM']:
        return 'MEDIUM'
    return 'LOW'


@app.route('/api/predict_thresholds')
def api_predict_thresholds():
    c = _build_calibration()
    return jsonify(dict(
        source=c['source'], rows=si(c.get('n', 0)),
        tier_percentiles=TIER_PERCENTILES,
        thresholds={k: round(v, 6) for k, v in c['thresholds'].items()},
    ))


# ================================================================
# API — LIVE PREDICTION (production XGBoost + SHAP)
# ================================================================
CURRENCIES = ['Australian Dollar', 'Bitcoin', 'Brazil Real', 'Canadian Dollar', 'Euro',
              'Mexican Peso', 'Ruble', 'Rupee', 'Saudi Riyal', 'Shekel', 'Swiss Franc',
              'UK Pound', 'US Dollar', 'Yen', 'Yuan']
FORMATS = ['ACH', 'Bitcoin', 'Cash', 'Cheque', 'Credit Card', 'Reinvestment', 'Wire']


@app.route('/api/predict_meta')
def api_predict_meta():
    return jsonify(dict(features=XGB_FEATURES, currencies=CURRENCIES, formats=FORMATS,
                        model_available=model_xgb is not None,
                        shap_available=shap_exp is not None))


@app.route('/api/predict', methods=['POST'])
def api_predict():
    if model_xgb is None:
        return jsonify({'error': 'XGBoost model is not loaded on the server.'}), 500
    try:
        data = request.get_json(force=True) or {}
        amount_paid = max(0.0, sf(data.get('amount_paid', 0)))
        amount_received = max(0.0, sf(data.get('amount_received', amount_paid)))
        fmt = data.get('payment_format', 'ACH')
        pay_cur = data.get('payment_currency', 'US Dollar')
        recv_cur = data.get('receiving_currency', 'US Dollar')

        row = {
            'Hour': sf(data.get('hour', 12), 0), 'Day': sf(data.get('day', 15), 0),
            'DayOfWeek': sf(data.get('day_of_week', 2), 0),
            'IsWeekend': 1.0 if data.get('is_weekend') else 0.0,
            'Sender Bank ID': sf(data.get('sender_bank', 10), 0),
            'Receiver Bank ID': sf(data.get('receiver_bank', 10), 0),
            'is_cross_bank': 1.0 if data.get('is_cross_bank') else 0.0,
            'is_self_transfer': 1.0 if data.get('is_self_transfer') else 0.0,
            'Amount_Paid_Log': float(np.log1p(amount_paid)),
            'Amount_Received_Log': float(np.log1p(amount_received)),
            'Amount_Diff_Log': float(np.log1p(abs(amount_paid - amount_received))),
            'amount_vs_account_median': sf(data.get('amount_vs_median_ratio', 1.0)),
            'Payment_Currency_Encoded': float(CURRENCIES.index(pay_cur)) if pay_cur in CURRENCIES else 12.0,
            'Receiving_Currency_Encoded': float(CURRENCIES.index(recv_cur)) if recv_cur in CURRENCIES else 12.0,
            'is_currency_mismatch': 1.0 if pay_cur != recv_cur else 0.0,
            'is_amount_outlier_iqr': 1.0 if data.get('is_amount_outlier_iqr') else 0.0,
            'is_amount_outlier_zscore': 1.0 if data.get('is_amount_outlier_zscore') else 0.0,
            'is_account_level_outlier': 1.0 if data.get('is_account_level_outlier') else 0.0,
            'is_high_velocity': 1.0 if data.get('is_high_velocity') else 0.0,
            'txn_count_per_sender_hour': sf(data.get('txn_count_per_hour', 1), 0),
            'txn_count_per_sender_day': sf(data.get('txn_count_per_day', 1), 0),
        }
        row['outlier_score'] = (row['is_amount_outlier_iqr'] + row['is_amount_outlier_zscore'] +
                                row['is_account_level_outlier'] + row['is_high_velocity'])
        row['is_outlier'] = 1.0 if row['outlier_score'] > 0 else 0.0
        for f in FORMATS:
            row[f'fmt_{f}'] = 1.0 if fmt == f else 0.0

        order = XGB_FEATURES if XGB_FEATURES else list(row.keys())
        inp = pd.DataFrame([[row.get(f, 0.0) for f in order]], columns=order)

        prob = float(model_xgb.predict_proba(inp)[0][1])
        # Percentile of this probability within all 5.08M scored transactions,
        # then the engine's top-0.5 / 2.5 / 10% tiers. See SCORE CALIBRATION.
        percentile = score_to_percentile(prob)
        score = round(percentile, 2)
        level = level_from_prob(prob)
        cal = _build_calibration()

        pairs, base_value = [], 0.0
        if shap_exp is not None:
            try:
                sv = np.array(shap_exp.shap_values(inp)).flatten()
                base_value = float(np.array(shap_exp.expected_value).flatten()[0])
                pairs = sorted(zip(order, sv, inp.iloc[0].tolist()),
                               key=lambda x: abs(x[1]), reverse=True)
            except Exception as se:
                print('SHAP error:', se)

        top = [dict(feature=f, shap_value=round(float(v), 4),
                    feature_value=round(float(fv), 4),
                    direction='fraud' if v > 0 else 'normal') for f, v, fv in pairs[:8]]
        primary = [t for t in top if t['direction'] == 'fraud'][:5]
        mitigating = [t for t in top if t['direction'] == 'normal'][:3]

        lines = ["SUSPICIOUS ACTIVITY REPORT — AUTO DRAFT", "Generated by: FinShield AI v1.0",
                 "-" * 60,
                 f"FRAUD PROBABILITY : {prob:.6f}",
                 f"RISK PERCENTILE   : {percentile:.2f}  (riskier than {percentile:.2f}% of all transactions)",
                 f"RISK LEVEL        : {level}",
                 f"SHAP BASE VALUE   : {base_value:.6f}", "-" * 60,
                 "PRIMARY RISK FACTORS (pushing toward FRAUD):"]
        lines += [f"  ► {t['feature']:<32} SHAP: {t['shap_value']:+.4f}" for t in primary] or ["  (none identified)"]
        lines += ["", "MITIGATING FACTORS (pushing toward NORMAL):"]
        lines += [f"  ▼ {t['feature']:<32} SHAP: {t['shap_value']:+.4f}" for t in mitigating] or ["  (none identified)"]
        lines += ["-" * 60,
                  f"DECISION : {'ESCALATE FOR ANALYST REVIEW' if level in ('HIGH', 'CRITICAL') else 'MONITOR'}",
                  "STATUS   : PENDING ANALYST REVIEW"]

        return jsonify(dict(
            fraud_probability=round(prob, 6), risk_score=score, percentile=round(percentile, 2),
            risk_level=level, base_value=round(base_value, 6),
            thresholds={k: round(v, 6) for k, v in cal['thresholds'].items()},
            calibration_source=cal['source'],
            top_factors=top, sar_narrative='\n'.join(lines)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ================================================================
if __name__ == '__main__':
    print("Dashboard → http://localhost:5000")
    print("Press CTRL+C to stop\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
