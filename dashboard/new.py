"""
FinShield AI — new.py
=====================
Extension server for the existing FinShield AI dashboard.

    python dashboard/new.py  →  http://localhost:5000

It IMPORTS the existing backend (app.py) instead of copying it, so every
existing tab and API keeps running from its own files, unchanged. new.py
only ADDS two sidebar tabs and the APIs behind them:

  User Portal
    • Screen a transaction      /api/portal/meta, /api/portal/score
    • Link Security             /api/check-url  (static phishing-link check;
                                skipped if app.py already provides it)
  Investigation Workbench       /api/wb/*
    • Alert queue + live counts /api/alerts (app.py), /api/wb/queue_stats
    • Case detail / lookup      /api/wb/alert, /api/wb/txn, /api/wb/account
    • Why flagged (SHAP)        /api/wb/explain
    • Linked accounts           /api/wb/network
    • Analyst workflow + audit  /api/wb/action, /api/wb/audit, /api/wb/cases
    • SAR report                /api/wb/sar  (auto-drafted Suspicious Activity
                                Report; "File SAR" is recorded in the audit log)
  Live Prediction fix           scaled input + calibrated tiers for /api/predict

and injects new.html (styles, nav items, pages) + new.js into the page that
app.py already serves.

Files (all in the same folder as app.py):
  new.py · new.html · new.js
Data it reads (existing project files, never modified):
  data/cleaned_transactions.csv     transaction details, accounts, network
  results/final_risk_scores.csv     Risk Engine score + level per transaction
  results/alerts.csv                alert queue (via app.py)
  models/xgboost_model.pkl          production model (via app.py)
Files it writes:
  data/precomputed/new_txn_index.npz / .json   transaction-index cache (rebuilt
                                             automatically if a source changes)
  results/analyst_audit_log.jsonl          analyst actions (audit trail)
"""
import os
import re
import json
import time
import threading
import datetime
import ipaddress
from collections import Counter
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from flask import request, jsonify, send_from_directory, Response

import app as base   # the existing FinShield AI backend (all existing tabs)

app = base.app
BASE = base.BASE
HERE = os.path.dirname(os.path.abspath(__file__))

# Names reused from app.py
sf = base.sf
CURRENCIES = base.CURRENCIES
FORMATS = base.FORMATS
model_xgb = base.model_xgb
shap_exp = base.shap_exp
XGB_FEATURES = base.XGB_FEATURES

# ================================================================
# API — USER PORTAL  (split-window transaction screening)
# ----------------------------------------------------------------
# Feature 1: Transaction Composer — the user describes a payment in plain
#            terms; the backend derives all 30 model features itself.
# Feature 2: Live Risk Verdict — risk percentile, decision
#            (APPROVE / REVIEW / HOLD / BLOCK) and plain-English reasons
#            built from exact TreeSHAP contributions.
#
# Self-contained: it does not change /api/predict. It carries its own
# scaler + calibration because models/xgboost_model.pkl was trained on
# RobustScaler output whose scaler was never saved (rebuilt exactly
# below — see claude/live-prediction-bug-fix.md in the project).
# ================================================================

PORTAL_SCALER = {                   # feature: (median, IQR); unlisted → (0, 1)
    'Hour': (10.0, 13.0), 'Day': (5.0, 6.0), 'DayOfWeek': (3.0, 2.0),
    'Sender Bank ID': (9679.0, 28509.0), 'Receiver Bank ID': (21568.0, 118073.0),
    'is_cross_bank': (1.0, 1.0),
    'Amount_Paid_Log': (7.253795878183534, 4.194045286842848),
    'Amount_Received_Log': (7.2512882441739235, 4.2039806699939515),
    'amount_vs_account_median': (0.9986932289658309, 1.660164798176843),
    'Payment_Currency_Encoded': (10.0, 8.0), 'Receiving_Currency_Encoded': (10.0, 8.0),
    'txn_count_per_sender_hour': (4.0, 5.0), 'txn_count_per_sender_day': (5.0, 8.0),
}

# Percentiles of correctly-scaled XGBoost probabilities
# (1,015,668-row held-out test set, results/xgb_predictions.csv).
PORTAL_QUANTILES = np.array([
    (0, 0.00002802), (10, 0.00031443), (20, 0.00050096), (30, 0.00067695),
    (40, 0.00086962), (50, 0.00110249), (60, 0.00142169), (70, 0.00197275),
    (75, 0.00255723), (80, 0.00513361), (85, 0.02554933), (90, 0.07445164),
    (92.5, 0.11698089), (95, 0.18975039), (96, 0.23380135), (97, 0.29576698),
    (97.5, 0.34120555), (98, 0.39880862), (98.5, 0.47313584), (99, 0.56929232),
    (99.25, 0.62758526), (99.5, 0.69482092), (99.75, 0.77924108),
    (99.9, 0.84976542), (99.99, 0.98454284), (100, 0.99851600),
])
PORTAL_TIERS = [('CRITICAL', 99.5), ('HIGH', 97.5), ('MEDIUM', 90.0)]

# Exact cut-offs used by the feature-engineering notebook (recovered from
# the saved features: every flagged row sits on the right side of these).
IQR_AMOUNT_FENCE = 30_469.36        # is_amount_outlier_iqr   (17.4% of txns)
ZSCORE_AMOUNT_FENCE = 2.6e9         # is_amount_outlier_zscore (0.02%)
ACCOUNT_OUTLIER_RATIO = 5.0         # is_account_level_outlier: > 5× own median
HIGH_VELOCITY_PER_HOUR = 10         # is_high_velocity: > 10 txns in the hour
DAY_NEUTRAL = 5.0                   # 'Day' leaks the label in IBM HI-Small

DECISIONS = {
    'LOW':      ('APPROVE', 'Release the payment. No action needed.'),
    'MEDIUM':   ('REVIEW',  'Release, but add to the monitoring watch-list.'),
    'HIGH':     ('HOLD',    'Hold the payment and send it to an analyst for review.'),
    'CRITICAL': ('BLOCK',   'Block the payment and escalate for a Suspicious Activity Report.'),
}

WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

PORTAL_PRESETS = [
    dict(id='salary', label='Salary credit', tone='good',
         desc='Monthly salary, same bank, business hours',
         values=dict(amount=4200, payment_format='ACH', send_currency='US Dollar',
                     receive_currency='US Dollar', sender_bank=1, receiver_bank=1,
                     hour=10, weekday=0, usual_amount=4000, txn_hour=1, txn_day=1)),
    dict(id='card', label='Card purchase', tone='good',
         desc='Everyday credit-card spend',
         values=dict(amount=86, payment_format='Credit Card', send_currency='US Dollar',
                     receive_currency='US Dollar', sender_bank=31, receiver_bank=31,
                     hour=18, weekday=4, usual_amount=120, txn_hour=1, txn_day=3)),
    dict(id='structuring', label='Structuring', tone='bad',
         desc='Just under $10k, cross-bank, repeated today',
         values=dict(amount=9850, payment_format='ACH', send_currency='US Dollar',
                     receive_currency='US Dollar', sender_bank=12, receiver_bank=70,
                     hour=15, weekday=2, usual_amount=1500, txn_hour=4, txn_day=9)),
    dict(id='burst', label='Layering burst', tone='bad',
         desc='Many fast cross-bank transfers in one hour',
         values=dict(amount=3000, payment_format='ACH', send_currency='US Dollar',
                     receive_currency='US Dollar', sender_bank=12, receiver_bank=70,
                     hour=2, weekday=5, usual_amount=400, txn_hour=14, txn_day=40)),
    dict(id='crypto', label='Crypto cash-out', tone='neutral',
         desc='Bitcoin payout to dollars — a known blind spot of the model',
         values=dict(amount=25000, payment_format='Bitcoin', send_currency='Bitcoin',
                     receive_currency='US Dollar', sender_bank=12, receiver_bank=220,
                     hour=3, weekday=6, usual_amount=800, txn_hour=3, txn_day=6)),
    dict(id='wire', label='Large wire', tone='neutral',
         desc='Big international wire, first of the day',
         values=dict(amount=250000, payment_format='Wire', send_currency='Euro',
                     receive_currency='US Dollar', sender_bank=12, receiver_bank=48,
                     hour=11, weekday=1, usual_amount=60000, txn_hour=1, txn_day=1)),
]


def _portal_percentile(prob):
    return float(np.clip(np.interp(prob, PORTAL_QUANTILES[:, 1], PORTAL_QUANTILES[:, 0]), 0, 100))


def _portal_level(pct):
    for name, cut in PORTAL_TIERS:
        if pct >= cut:
            return name
    return 'LOW'


def _portal_shap(inp_scaled, order):
    """Exact TreeSHAP in log-odds space: the saved explainer if present,
    otherwise XGBoost's built-in pred_contribs (same algorithm)."""
    if shap_exp is not None:
        sv = np.array(shap_exp.shap_values(inp_scaled)).reshape(-1)[:len(order)]
        return sv, float(np.array(shap_exp.expected_value).reshape(-1)[0])
    import xgboost as _xgb
    dm = _xgb.DMatrix(inp_scaled, feature_names=list(order))
    contrib = np.asarray(model_xgb.get_booster().predict(dm, pred_contribs=True)).reshape(-1)
    return contrib[:-1], float(contrib[-1])


def _fmt_money(v, cur):
    sym = {'US Dollar': '$', 'Euro': '€', 'UK Pound': '£', 'Rupee': '₹', 'Yen': '¥', 'Yuan': '¥'}.get(cur)
    return f"{sym}{v:,.0f}" if sym else f"{v:,.2f} {cur}"


def _portal_reasons(shap_by_feat, ctx):
    """Group the 30 SHAP values into the handful of things a person can
    reason about, and write each as a sentence."""
    fmt_, amt, cur = ctx['payment_format'], ctx['amount'], ctx['send_currency']
    groups = [
        ('method', 'Payment method',
         [f for f in shap_by_feat if f.startswith('fmt_')],
         f"{fmt_} payment" + (" — the format used in most laundering chains in the training data"
                              if fmt_ == 'ACH' else "")),
        ('amount', 'Amount',
         ['Amount_Paid_Log', 'Amount_Received_Log', 'Amount_Diff_Log', 'is_amount_outlier_iqr',
          'is_amount_outlier_zscore'],
         f"{_fmt_money(amt, cur)}" + (" — above the dataset's large-amount fence (30,470 in the paid currency)"
                                       if ctx['flags']['large_amount'] else "")),
        ('behaviour', 'Compared with sender\'s usual',
         ['amount_vs_account_median', 'is_account_level_outlier'],
         f"{ctx['ratio']:.1f}× the sender's usual amount" +
         (" — a personal outlier (5×+)" if ctx['flags']['personal_outlier'] else "")),
        ('velocity', 'Transaction velocity',
         ['txn_count_per_sender_hour', 'txn_count_per_sender_day', 'is_high_velocity'],
         f"{ctx['txn_hour']} transfers this hour, {ctx['txn_day']} today" +
         (" — high velocity (10+/hr)" if ctx['flags']['high_velocity'] else "")),
        ('route', 'Bank route',
         ['is_cross_bank', 'Sender Bank ID', 'Receiver Bank ID', 'is_self_transfer'],
         ("Sends to the sender's own account" if ctx['self_transfer'] else
          ("Cross-bank transfer" if ctx['cross_bank'] else "Stays within the same bank")) +
         f" (bank {ctx['sender_bank']} → {ctx['receiver_bank']})"),
        ('currency', 'Currency',
         ['Payment_Currency_Encoded', 'Receiving_Currency_Encoded', 'is_currency_mismatch'],
         (f"Converted {cur} → {ctx['receive_currency']}" if ctx['currency_mismatch']
          else f"Paid and received in {cur}")),
        ('timing', 'Timing',
         ['Hour', 'DayOfWeek', 'IsWeekend'],
         f"{WEEKDAYS[ctx['weekday']]} at {ctx['hour']:02d}:00" +
         (" (weekend)" if ctx['weekday'] >= 5 else "") +
         (" — outside business hours" if ctx['hour'] < 6 or ctx['hour'] >= 22 else "")),
        ('outliers', 'Combined outlier signals',
         ['outlier_score', 'is_outlier'],
         f"{ctx['outlier_score']} of 4 outlier checks triggered"),
    ]
    out = []
    for key, title, feats, detail in groups:
        impact = float(sum(shap_by_feat.get(f, 0.0) for f in feats))
        out.append(dict(key=key, title=title, detail=detail, impact=round(impact, 4),
                        direction='risk' if impact > 0 else 'safe'))
    out.sort(key=lambda r: abs(r['impact']), reverse=True)
    return out


@app.route('/api/portal/meta')
def api_portal_meta():
    return jsonify(dict(currencies=CURRENCIES, formats=FORMATS, weekdays=WEEKDAYS,
                        presets=PORTAL_PRESETS, model_available=model_xgb is not None,
                        tiers=[dict(level=n, percentile=p) for n, p in PORTAL_TIERS]))


@app.route('/api/portal/score', methods=['POST'])
def api_portal_score():
    if model_xgb is None:
        return jsonify({'error': 'XGBoost model is not loaded on the server.'}), 500
    try:
        d = request.get_json(force=True) or {}
        amount = max(0.0, sf(d.get('amount', 0), 2))
        fmt_ = d.get('payment_format', 'ACH')
        fmt_ = fmt_ if fmt_ in FORMATS else 'ACH'
        send_cur = d.get('send_currency', 'US Dollar')
        recv_cur = d.get('receive_currency', send_cur)
        send_cur = send_cur if send_cur in CURRENCIES else 'US Dollar'
        recv_cur = recv_cur if recv_cur in CURRENCIES else send_cur
        sender_bank = int(max(0, sf(d.get('sender_bank', 10), 0)))
        receiver_bank = int(max(0, sf(d.get('receiver_bank', sender_bank), 0)))
        self_transfer = bool(d.get('self_transfer'))
        if self_transfer:
            receiver_bank = sender_bank
        hour = int(np.clip(sf(d.get('hour', 12), 0), 0, 23))
        weekday = int(np.clip(sf(d.get('weekday', 2), 0), 0, 6))
        usual = sf(d.get('usual_amount', 0), 2)
        usual = usual if usual > 0 else max(amount, 1.0)
        txn_hour = int(max(1, sf(d.get('txn_hour', 1), 0)))
        txn_day = int(max(txn_hour, sf(d.get('txn_day', txn_hour), 0)))

        cross_bank = sender_bank != receiver_bank
        ratio = amount / usual if usual > 0 else 1.0
        flags = dict(large_amount=amount > IQR_AMOUNT_FENCE,
                     extreme_amount=amount > ZSCORE_AMOUNT_FENCE,
                     personal_outlier=ratio > ACCOUNT_OUTLIER_RATIO,
                     high_velocity=txn_hour > HIGH_VELOCITY_PER_HOUR)
        outlier_score = int(sum(flags.values()))

        row = {
            'Hour': hour, 'Day': DAY_NEUTRAL, 'DayOfWeek': weekday,
            'IsWeekend': 1.0 if weekday >= 5 else 0.0,
            'Sender Bank ID': sender_bank, 'Receiver Bank ID': receiver_bank,
            'is_cross_bank': 1.0 if cross_bank else 0.0,
            'is_self_transfer': 1.0 if self_transfer else 0.0,
            'Amount_Paid_Log': float(np.log1p(amount)),
            'Amount_Received_Log': float(np.log1p(amount)),
            'Amount_Diff_Log': 0.0,
            'amount_vs_account_median': ratio,
            'Payment_Currency_Encoded': float(CURRENCIES.index(send_cur)),
            'Receiving_Currency_Encoded': float(CURRENCIES.index(recv_cur)),
            'is_currency_mismatch': 1.0 if send_cur != recv_cur else 0.0,
            'is_amount_outlier_iqr': float(flags['large_amount']),
            'is_amount_outlier_zscore': float(flags['extreme_amount']),
            'is_account_level_outlier': float(flags['personal_outlier']),
            'outlier_score': float(outlier_score),
            'is_outlier': 1.0 if outlier_score else 0.0,
            'txn_count_per_sender_hour': txn_hour, 'txn_count_per_sender_day': txn_day,
            'is_high_velocity': float(flags['high_velocity']),
        }
        for f in FORMATS:
            row[f'fmt_{f}'] = 1.0 if fmt_ == f else 0.0

        order = XGB_FEATURES if XGB_FEATURES else list(row.keys())
        raw = pd.DataFrame([[float(row.get(f, 0.0)) for f in order]], columns=order)
        scaled = raw.copy()
        for f in order:
            c, s = PORTAL_SCALER.get(f, (0.0, 1.0))
            scaled[f] = (scaled[f] - c) / s

        prob = float(model_xgb.predict_proba(scaled)[0][1])
        pct = _portal_percentile(prob)
        level = _portal_level(pct)
        decision, action = DECISIONS[level]

        ctx = dict(payment_format=fmt_, amount=amount, send_currency=send_cur,
                   receive_currency=recv_cur, ratio=ratio, txn_hour=txn_hour, txn_day=txn_day,
                   self_transfer=self_transfer, cross_bank=cross_bank,
                   sender_bank=sender_bank, receiver_bank=receiver_bank,
                   currency_mismatch=send_cur != recv_cur, hour=hour, weekday=weekday,
                   flags=flags, outlier_score=outlier_score)
        reasons, base_value, shap_ok = [], None, True
        try:
            sv, base_value = _portal_shap(scaled, order)
            reasons = _portal_reasons(dict(zip(order, map(float, sv))), ctx)
        except Exception as se:
            shap_ok = False
            print('Portal SHAP error:', se)

        derived = [
            dict(label='Route', value='Self-transfer' if self_transfer else
                 ('Cross-bank' if cross_bank else 'Same bank'), alert=cross_bank),
            dict(label='vs usual amount', value=f"{ratio:.1f}×", alert=flags['personal_outlier']),
            dict(label='Large amount', value='Yes' if flags['large_amount'] else 'No',
                 alert=flags['large_amount']),
            dict(label='Velocity', value=f"{txn_hour}/hr · {txn_day}/day", alert=flags['high_velocity']),
            dict(label='Currency', value='Converted' if send_cur != recv_cur else 'Same',
                 alert=send_cur != recv_cur),
            dict(label='Outlier checks', value=f"{outlier_score} / 4", alert=outlier_score >= 2),
        ]

        return jsonify(dict(
            fraud_probability=round(prob, 6), percentile=round(pct, 2), risk_level=level,
            decision=decision, action=action,
            headline=f"Riskier than {pct:.1f}% of transactions the model has scored",
            tiers=[dict(level=n, percentile=p) for n, p in PORTAL_TIERS],
            reasons=reasons, base_value=base_value, shap_available=shap_ok,
            derived=derived,
            notes=["Day-of-month is not used: in the IBM dataset days 11–18 are almost all "
                   "laundering, so it is a data artefact rather than a risk signal."]))
    except Exception as e:
        return jsonify({'error': str(e)}), 500



def _scale_frame(raw):
    """Apply the production model's training RobustScaler (PORTAL_SCALER)."""
    out = raw.astype(float).copy()
    for f in out.columns:
        c, s = PORTAL_SCALER.get(f, (0.0, 1.0))
        out[f] = (out[f] - c) / s
    return out


# ================================================================
# LIVE PREDICTION FIX  (existing /api/predict, /api/predict_meta,
# /api/predict_thresholds keep their URLs; only their maths is fixed)
# ----------------------------------------------------------------
# The production XGBoost was trained on RobustScaler output whose scaler was
# never saved; app.py scored raw values and calibrated against scores that
# were produced the same way, so almost everything came out CRITICAL.
# ================================================================
def _build_calibration_fixed():
    cal = base._CAL
    if cal.get('_scaled'):
        return cal
    cal.clear()
    pcts = PORTAL_QUANTILES[:, 0].astype(float)
    vals = PORTAL_QUANTILES[:, 1].astype(float)
    cal.update(pcts=pcts, vals=vals, n=1_015_668, _scaled=True,
               source='scaled XGBoost, 1,015,668-row held-out test set')
    cal['thresholds'] = {t: float(np.interp(q, pcts, vals))
                         for t, q in base.TIER_PERCENTILES.items()}
    return cal


# score_to_percentile / level_from_prob / api_predict_thresholds in app.py
# look this name up at call time, so replacing it fixes all three.
base._build_calibration = _build_calibration_fixed


def api_predict_fixed():
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
            'Hour': sf(data.get('hour', 12), 0), 'Day': DAY_NEUTRAL,
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
        inp_raw = pd.DataFrame([[row.get(f, 0.0) for f in order]], columns=order)
        inp = _scale_frame(inp_raw)
        prob = float(model_xgb.predict_proba(inp)[0][1])
        percentile = base.score_to_percentile(prob)
        level = base.level_from_prob(prob)
        cal = _build_calibration_fixed()

        pairs, base_value = [], 0.0
        try:
            sv, base_value = _portal_shap(inp, order)
            pairs = sorted(zip(order, sv, inp_raw.iloc[0].tolist()),
                           key=lambda x: abs(x[1]), reverse=True)
        except Exception as se:
            print('SHAP error:', se)

        top = [dict(feature=f, shap_value=round(float(v), 4), feature_value=round(float(fv), 4),
                    direction='fraud' if v > 0 else 'normal') for f, v, fv in pairs[:8]]
        primary = [t for t in top if t['direction'] == 'fraud'][:5]
        mitigating = [t for t in top if t['direction'] == 'normal'][:3]
        lines = ["SUSPICIOUS ACTIVITY REPORT — AUTO DRAFT", "Generated by: FinShield AI v1.0",
                 "-" * 60, f"FRAUD PROBABILITY : {prob:.6f}",
                 f"RISK PERCENTILE   : {percentile:.2f}  (riskier than {percentile:.2f}% of scored transactions)",
                 f"RISK LEVEL        : {level}", f"SHAP BASE VALUE   : {base_value:.6f}", "-" * 60,
                 "PRIMARY RISK FACTORS (pushing toward FRAUD):"]
        lines += [f"  ► {t['feature']:<32} SHAP: {t['shap_value']:+.4f}" for t in primary] or ["  (none identified)"]
        lines += ["", "MITIGATING FACTORS (pushing toward NORMAL):"]
        lines += [f"  ▼ {t['feature']:<32} SHAP: {t['shap_value']:+.4f}" for t in mitigating] or ["  (none identified)"]
        lines += ["-" * 60,
                  f"DECISION : {'ESCALATE FOR ANALYST REVIEW' if level in ('HIGH', 'CRITICAL') else 'MONITOR'}",
                  "STATUS   : PENDING ANALYST REVIEW"]
        return jsonify(dict(
            fraud_probability=round(prob, 6), risk_score=round(percentile, 2),
            percentile=round(percentile, 2), risk_level=level, base_value=round(base_value, 6),
            thresholds={k: round(v, 6) for k, v in cal['thresholds'].items()},
            calibration_source=cal['source'],
            notes=["Day-of-month is held at the training median (5): in the IBM dataset "
                   "days 11–18 are almost entirely laundering."],
            top_factors=top, sar_narrative='\n'.join(lines)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def api_predict_meta_fixed():
    return jsonify(dict(features=XGB_FEATURES, currencies=CURRENCIES, formats=FORMATS,
                        model_available=model_xgb is not None,
                        shap_available=model_xgb is not None))


if 'api_predict' in app.view_functions:
    app.view_functions['api_predict'] = api_predict_fixed
if 'api_predict_meta' in app.view_functions:
    app.view_functions['api_predict_meta'] = api_predict_meta_fixed


# ================================================================
# API — LINK SECURITY  (POST /api/check-url)
# ----------------------------------------------------------------
# Static, defensive phishing-link check. The server only inspects the
# text of the link — it never opens, fetches or DNS-resolves it, so a
# submitted URL can't make the server reach anything. A LOW result means
# no structural red flags were found, not that the site is safe.
# ================================================================
LINK_KEYWORDS = ['login', 'verify', 'verification', 'account', 'secure', 'security',
                 'update', 'password', 'otp', 'kyc', 'bank', 'wallet', 'payment',
                 'refund', 'claim', 'urgent', 'suspended', 'blocked']
LINK_DOMAIN_WORDS = ['verify', 'secure', 'account', 'login', 'update', 'banking']
LINK_SHORTENERS = {'bit.ly', 'tinyurl.com', 't.co', 'goo.gl', 'ow.ly', 'is.gd', 'buff.ly',
                   'rebrand.ly', 'cutt.ly', 'shorturl.at', 'tiny.cc', 'rb.gy'}
LINK_ENCODINGS = ('%40', '%2f', '%3a')          # percent-encoded @ / :
LINK_MAX_LEN = 2048
LINK_TIERS = [
    ('HIGH', 75, 'Do not open the link or enter a password, OTP, UPI PIN or card details.'),
    ('MEDIUM', 40, 'Verify the domain and where the link really goes before entering anything sensitive.'),
    ('LOW', 0, 'No major structural red flags. This does not guarantee the site is safe.'),
]


class LinkInputError(ValueError):
    """The submitted text is not a checkable http(s) URL."""


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def analyze_url(url):
    url = str(url).strip()
    has_scheme = re.match(r'^[a-z][a-z0-9+.-]*://', url, re.I) is not None
    try:
        parsed = urlparse(url if has_scheme else 'https://' + url)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError:
        raise LinkInputError('That is not a valid URL.')
    if parsed.scheme.lower() not in ('http', 'https'):
        raise LinkInputError('Only http:// and https:// links can be checked.')
    if not host or not (_is_ip(host) or '.' in host or host == 'localhost'):
        raise LinkInputError('That is not a valid URL — no domain name was found.')

    indicators = []

    def flag(points, reason):
        indicators.append(dict(points=points, reason=reason))

    is_ip = _is_ip(host)
    if has_scheme and parsed.scheme.lower() != 'https':
        flag(15, 'Does not use HTTPS — anything typed into this site is sent unencrypted')
    if is_ip:
        flag(30, 'Uses a raw IP address instead of a domain name')
    if '@' in parsed.netloc:
        flag(25, 'Contains "@" before the domain, which can hide the real destination')
    if host.startswith('xn--') or '.xn--' in host:
        flag(25, 'Punycode domain (xn--) — can imitate a real brand with look-alike letters')
    if host in LINK_SHORTENERS:
        flag(15, 'Link shortener — the real destination is hidden')
    if port is not None and port not in (80, 443):
        flag(10, f'Uses non-standard port {port}')
    if len(url) > 120:
        flag(10, f'Unusually long URL ({len(url)} characters)')
    labels = [p for p in host.split('.') if p]
    if not is_ip and len(labels) >= 4:
        flag(15, f'Deep subdomain structure ({len(labels)} levels)')
    if host.count('-') >= 3:
        flag(10, 'Domain name contains many hyphens')
    low = url.lower()
    if any(e in low for e in LINK_ENCODINGS):
        flag(10, 'Percent-encoded @, / or : that can disguise the URL')
    words = [w for w in LINK_DOMAIN_WORDS if w in host]
    if words:
        flag(10, 'Domain name uses trust words: ' + ', '.join(words))
    kws = [k for k in LINK_KEYWORDS if k in low]
    if kws:
        flag(min(20, 4 * len(kws)), 'Phishing keywords in the link: ' + ', '.join(kws[:5]))

    score = min(100, sum(i['points'] for i in indicators))
    level, _, advice = next(t for t in LINK_TIERS if score >= t[1])
    notes = ['Only the text of the link was checked; the website was not opened.']
    if not has_scheme:
        notes.append('No http:// or https:// was given, so it was checked as https://.')
    return dict(url=url, host=host, risk_score=score, risk_level=level,
                reasons=[i['reason'] for i in indicators], indicators=indicators,
                recommendation=advice, notes=notes)


def api_check_url():
    data = request.get_json(silent=True) or {}
    url = str(data.get('url', '')).strip()
    if not url:
        return jsonify(success=False, error='Enter a URL to check.'), 400
    if len(url) > LINK_MAX_LEN:
        return jsonify(success=False, error=f'URL is too long (max {LINK_MAX_LEN} characters).'), 400
    try:
        result = analyze_url(url)
    except LinkInputError as ex:
        return jsonify(success=False, error=str(ex)), 400
    return jsonify(success=True, **result)


if not any(r.rule == '/api/check-url' for r in app.url_map.iter_rules()):
    app.add_url_rule('/api/check-url', 'check_url', api_check_url, methods=['POST'])


# ================================================================
# TRANSACTION INDEX
# ----------------------------------------------------------------
# One compact, in-memory index over data/cleaned_transactions.csv:
#   • byte offset of every row  → O(1) lookup of any transaction's full row
#   • sender / receiver / amounts / currencies / day / hour / label arrays
#     → account search, history and counterparty network
#   • Risk Engine score + level per row from results/final_risk_scores.csv,
#     attached only after verifying row-by-row alignment
#   • alert → transaction row, by exact match of the alert's stored values
#     against final_risk_scores.csv (written in the same Risk Engine run)
# The Transaction ID is the row number in cleaned_transactions.csv
# (shown as TX-0000123). Built once in the background, then cached.
# ================================================================
CLEANED_PATH = os.path.join(BASE, 'data', 'cleaned_transactions.csv')
RISK_PATH = os.path.join(BASE, 'results', 'final_risk_scores.csv')
CACHE_DIR = os.path.join(BASE, 'data', 'precomputed')
CACHE_NPZ = os.path.join(CACHE_DIR, 'new_txn_index.npz')
CACHE_META = os.path.join(CACHE_DIR, 'new_txn_index.json')
INDEX_VERSION = 3

LEVELS = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']
LEVEL_CODE = {n: i for i, n in enumerate(LEVELS)}
INDEX_COLS = {                       # column in cleaned_transactions.csv → array name
    'Sender_Account_Encoded': 'sender', 'Receiver_Account_Encoded': 'receiver',
    'Amount Paid': 'paid', 'Amount Received': 'received',
    'Payment_Currency_Encoded': 'pay_cur', 'Receiving_Currency_Encoded': 'recv_cur',
    'Day': 'day', 'Hour': 'hour', 'Is Laundering': 'label',
}
ARRAY_DTYPES = dict(sender=np.int32, receiver=np.int32, paid=np.float64, received=np.float64,
                    pay_cur=np.int16, recv_cur=np.int16, day=np.int16, hour=np.int16, label=np.int8)
MODEL_DIRECT = ['Hour', 'Day', 'DayOfWeek', 'IsWeekend', 'Sender Bank ID', 'Receiver Bank ID',
                'is_cross_bank', 'is_self_transfer', 'amount_vs_account_median',
                'Payment_Currency_Encoded', 'Receiving_Currency_Encoded', 'is_currency_mismatch',
                'is_amount_outlier_iqr', 'is_amount_outlier_zscore', 'is_account_level_outlier',
                'outlier_score', 'txn_count_per_sender_hour', 'txn_count_per_sender_day',
                'is_high_velocity', 'Amount Paid', 'Amount Received']


def _sig(path):
    try:
        st = os.stat(path)
        return [os.path.abspath(path), st.st_size, int(st.st_mtime)]
    except OSError:
        return None


def _read_header(path):
    with open(path, 'rb') as fh:
        line = fh.readline().decode('utf-8-sig').rstrip('\r\n')
    return [c.strip().strip('"') for c in line.split(',')]


def _alerts_path():
    e = base.CSV_REGISTRY.get('alerts') if hasattr(base, 'CSV_REGISTRY') else None
    return e['path'] if e else None


class TxnIndex:
    def __init__(self):
        self.lock = threading.Lock()
        self.state, self.stage, self.progress, self.message = 'idle', '', 0.0, ''
        self.header, self.col = [], {}
        self.n = 0
        self.a = {}                     # arrays
        self.risk_available = False
        self.risk_note = ''
        self.alert_stats = {}
        self.alert_row = {}             # alert_id -> row
        self.row_alert = {}             # row -> alert_id
        self.alert_ambiguous = set()
        self.model_ready, self.model_missing = False, []
        self.built_from_cache = False
        self.build_seconds = 0.0

    # ---------------- public ----------------
    def start(self):
        if self.state in ('building', 'ready'):
            return
        self.state = 'building'
        self.pid = os.getpid()
        self.thread = threading.Thread(target=self._build, name='txn-index', daemon=True)
        self.thread.start()

    def ensure(self):
        """Restart the build if it was started in another process (e.g. gunicorn
        --preload forks after import, and threads do not survive a fork)."""
        if self.state == 'building' and (getattr(self, 'pid', None) != os.getpid()
                                         or not self.thread.is_alive()):
            self.state = 'idle'
            self.start()

    def status(self):
        return dict(state=self.state, stage=self.stage, progress=round(self.progress, 3),
                    message=self.message, rows=int(self.n), source='data/cleaned_transactions.csv',
                    risk_available=self.risk_available, risk_note=self.risk_note,
                    alerts=self.alert_stats, explain_available=self.model_ready and model_xgb is not None,
                    explain_missing=self.model_missing, from_cache=self.built_from_cache,
                    build_seconds=round(self.build_seconds, 1))

    def ready(self):
        return self.state == 'ready'

    # ---------------- build ----------------
    def _set(self, stage, progress, message=''):
        self.stage, self.progress = stage, float(max(0.0, min(1.0, progress)))
        if message:
            self.message = message

    def _build(self):
        t0 = time.time()
        try:
            if not os.path.isfile(CLEANED_PATH):
                self.state = 'unavailable'
                self.message = ('data/cleaned_transactions.csv not found — transaction lookup, '
                                'account search and the network need it.')
                return
            self.header = _read_header(CLEANED_PATH)
            self.col = {c: i for i, c in enumerate(self.header)}
            missing = [c for c in INDEX_COLS if c not in self.col]
            if missing:
                self.state = 'unavailable'
                self.message = f'cleaned_transactions.csv is missing columns: {missing}'
                return
            fmt_cols = [f'fmt_{f}' for f in FORMATS if f != 'Credit Card']
            self.model_missing = [c for c in MODEL_DIRECT + fmt_cols if c not in self.col]
            self.model_ready = not self.model_missing

            sig = dict(version=INDEX_VERSION, cleaned=_sig(CLEANED_PATH), risk=_sig(RISK_PATH),
                       alerts=_sig(_alerts_path()) if _alerts_path() else None,
                       alerts_rows=int(len(base.alerts_df)) if base.alerts_df is not None else 0)
            if not self._load_cache(sig):
                self._build_fresh()
                self._save_cache(sig)
            self._build_lookups()
            self.build_seconds = time.time() - t0
            self._set('ready', 1.0, f'{self.n:,} transactions indexed')
            self.state = 'ready'
            print(f"  ✓ transaction index ready — {self.n:,} rows "
                  f"({'cache' if self.built_from_cache else 'built'} in {self.build_seconds:.1f}s)")
        except Exception as ex:
            self.state = 'error'
            self.message = f'{type(ex).__name__}: {ex}'
            print('  ✗ transaction index failed:', self.message)

    def _build_fresh(self):
        # 1. byte offset of every row
        size = os.path.getsize(CLEANED_PATH)
        parts, pos = [], 0
        with open(CLEANED_PATH, 'rb') as fh:
            head = fh.readline()
            pos = len(head)
            parts.append(np.array([pos], dtype=np.int64))
            while True:
                chunk = fh.read(1 << 26)
                if not chunk:
                    break
                nl = np.flatnonzero(np.frombuffer(chunk, dtype=np.uint8) == 10).astype(np.int64)
                parts.append(nl + pos + 1)
                pos += len(chunk)
                self._set('Scanning transaction rows', 0.25 * pos / size)
        starts = np.concatenate(parts)
        starts = starts[starts < size]
        # 2. compact columns
        cols = list(INDEX_COLS)
        chunks = {k: [] for k in INDEX_COLS.values()}
        done = 0
        for ch in pd.read_csv(CLEANED_PATH, usecols=cols, chunksize=500_000):
            for c, name in INDEX_COLS.items():
                chunks[name].append(ch[c].to_numpy().astype(ARRAY_DTYPES[name]))
            done += len(ch)
            self._set('Reading transactions', 0.25 + 0.45 * done / max(1, len(starts)))
        a = {k: np.concatenate(v) for k, v in chunks.items()}
        n = len(a['sender'])
        if len(starts) != n:
            # blank trailing lines etc. — keep the first n non-empty offsets
            if len(starts) > n:
                starts = starts[:n]
            else:
                raise ValueError(f'row offset count {len(starts)} != rows {n}')
        a['offset'] = starts
        self.a, self.n = a, n
        self._attach_risk_fresh()

    def _attach_risk_fresh(self):
        a, n = self.a, self.n
        self.risk_available, self.risk_note = False, ''
        a['risk_score'] = np.full(n, np.nan, dtype=np.float32)
        a['risk_level'] = np.full(n, -1, dtype=np.int8)
        a['xgb_stored'] = np.full(n, np.nan, dtype=np.float32)
        a['alert_rows'] = np.zeros(0, dtype=np.int64)
        a['alert_ambig'] = np.zeros(0, dtype=np.int8)
        if not os.path.isfile(RISK_PATH):
            self.risk_note = 'results/final_risk_scores.csv not found — risk scores unavailable.'
            return
        rh = _read_header(RISK_PATH)
        need = ['Is Laundering', 'Sender_Account_Encoded', 'risk_score_100', 'risk_level']
        if any(c not in rh for c in need):
            self.risk_note = f'final_risk_scores.csv lacks {[c for c in need if c not in rh]}'
            return
        extra = [c for c in ('xgb_score', 'Amount_Paid_Log', 'ensemble_raw', 'composite_score') if c in rh]
        pos, mismatches, cand = 0, 0, []
        for ch in pd.read_csv(RISK_PATH, usecols=need + extra, chunksize=500_000):
            m = len(ch)
            if pos + m > n:
                mismatches += 1
                break
            lab = ch['Is Laundering'].to_numpy()
            snd = ch['Sender_Account_Encoded'].to_numpy()
            mismatches += int((lab != a['label'][pos:pos + m]).sum() +
                              (snd != a['sender'][pos:pos + m]).sum())
            a['risk_score'][pos:pos + m] = ch['risk_score_100'].to_numpy(dtype=np.float32)
            a['risk_level'][pos:pos + m] = ch['risk_level'].map(LEVEL_CODE).fillna(-1).to_numpy(dtype=np.int8)
            if 'xgb_score' in ch:
                a['xgb_stored'][pos:pos + m] = ch['xgb_score'].to_numpy(dtype=np.float32)
            hi = ch['risk_level'].isin(['HIGH', 'CRITICAL']).to_numpy()
            if hi.any():
                sub = ch.loc[hi, ['Sender_Account_Encoded', 'risk_score_100'] + extra].copy()
                sub['_row'] = np.flatnonzero(hi) + pos
                cand.append(sub)
            pos += m
            self._set('Attaching Risk Engine scores', 0.70 + 0.25 * pos / n)
        if pos != n or mismatches:
            a['risk_score'][:] = np.nan
            a['risk_level'][:] = -1
            a['xgb_stored'][:] = np.nan
            self.risk_note = ('final_risk_scores.csv rows do not line up with cleaned_transactions.csv '
                              f'({mismatches:,} label/sender mismatches, {pos:,} vs {n:,} rows) — '
                              'scores not attached.')
            return
        self.risk_available = True
        self.risk_note = 'Risk Engine output (results/final_risk_scores.csv), verified row-by-row.'
        self._map_alerts(pd.concat(cand, ignore_index=True) if cand else None, extra)

    def _map_alerts(self, cand, extra):
        a = self.a
        al = base.alerts_df
        if cand is None or al is None or al.empty or 'alert_id' not in al.columns:
            return
        keys = ['Sender_Account_Encoded', 'risk_score_100'] + [c for c in extra if c in al.columns]
        left = al[keys].copy()
        left['_i'] = np.arange(len(left))
        left['_occ'] = left.groupby(keys, sort=False).cumcount()
        grp = left.groupby(keys, sort=False)['_i'].transform('size').to_numpy()
        right = cand[keys + ['_row']].copy()
        right['_occ'] = right.groupby(keys, sort=False).cumcount()
        m = left.merge(right, on=keys + ['_occ'], how='left').sort_values('_i')
        rows = m['_row'].fillna(-1).to_numpy(dtype=np.int64)
        a['alert_rows'] = rows
        a['alert_ambig'] = (grp > 1).astype(np.int8)

    # ---------------- cache ----------------
    def _load_cache(self, sig):
        try:
            if not (os.path.isfile(CACHE_NPZ) and os.path.isfile(CACHE_META)):
                return False
            with open(CACHE_META, encoding='utf-8') as fh:
                meta = json.load(fh)
            if meta.get('sig') != json.loads(json.dumps(sig)):
                return False
            self._set('Loading cached index', 0.5)
            with np.load(CACHE_NPZ) as z:
                self.a = {k: z[k] for k in z.files}
            self.n = int(meta['n'])
            self.risk_available = bool(meta['risk_available'])
            self.risk_note = meta.get('risk_note', '')
            self.built_from_cache = True
            return True
        except Exception as ex:
            print('  ! index cache ignored:', ex)
            return False

    def _save_cache(self, sig):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = CACHE_NPZ + '.tmp.npz'
            np.savez(tmp, **self.a)
            os.replace(tmp, CACHE_NPZ)
            with open(CACHE_META, 'w', encoding='utf-8') as fh:
                json.dump(dict(sig=sig, n=self.n, risk_available=self.risk_available,
                               risk_note=self.risk_note), fh)
        except Exception as ex:
            print('  ! could not write index cache:', ex)

    # ---------------- derived lookups ----------------
    def _build_lookups(self):
        self._set('Building account indexes', 0.97)
        a = self.a
        n_acc = int(max(a['sender'].max(initial=0), a['receiver'].max(initial=0))) + 1
        self.n_accounts = n_acc
        for side in ('sender', 'receiver'):
            order = np.argsort(a[side], kind='stable').astype(np.int32)
            counts = np.bincount(a[side], minlength=n_acc)
            a[f'{side}_order'] = order
            a[f'{side}_start'] = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        a['time_key'] = (a['day'].astype(np.int32) * 24 + a['hour'].astype(np.int32))
        rows = a.get('alert_rows', np.zeros(0, dtype=np.int64))
        ambig = a.get('alert_ambig', np.zeros(0, dtype=np.int8))
        self.alert_row, self.row_alert, self.alert_ambiguous = {}, {}, set()
        al = base.alerts_df
        mapped = amb = 0
        if len(rows) and al is not None and len(al) == len(rows):
            ids = al['alert_id'].astype(str).tolist()
            for i, (aid, r) in enumerate(zip(ids, rows.tolist())):
                if r >= 0:
                    self.alert_row[aid] = r
                    self.row_alert.setdefault(r, aid)
                    mapped += 1
                    if ambig[i]:
                        self.alert_ambiguous.add(aid)
                        amb += 1
        total = int(len(al)) if al is not None else 0
        self.alert_stats = dict(total=total, mapped=mapped, ambiguous=amb, unmatched=total - mapped)

    # ---------------- queries ----------------
    def rows_for(self, acct, side):
        a = self.a
        if acct < 0 or acct >= self.n_accounts:
            return np.zeros(0, dtype=np.int32)
        s, e = a[f'{side}_start'][acct], a[f'{side}_start'][acct + 1]
        return a[f'{side}_order'][s:e]

    def fetch(self, rows):
        """Full parsed rows from cleaned_transactions.csv, by byte offset."""
        out = []
        offs = self.a['offset']
        with open(CLEANED_PATH, 'rb') as fh:
            for r in rows:
                fh.seek(int(offs[int(r)]))
                vals = fh.readline().decode('utf-8').rstrip('\r\n').split(',')
                d = {}
                for c, v in zip(self.header, vals):
                    try:
                        d[c] = float(v)
                    except ValueError:
                        # the fmt_* one-hot columns are stored as True/False text;
                        # left as strings they never equal 1, so every row looked
                        # like it had no payment method and SHAP saw all-zero formats
                        d[c] = _CSV_BOOL.get(v, v)
                out.append(d)
        return out


_CSV_BOOL = {'True': 1.0, 'False': 0.0, 'true': 1.0, 'false': 0.0}
TXN = TxnIndex()


# ---------------- helpers ----------------
def _cur(i):
    try:
        i = int(i)
        return CURRENCIES[i] if 0 <= i < len(CURRENCIES) else f'currency #{i}'
    except (TypeError, ValueError):
        return '—'


def _fmt_from_row(r):
    for f in FORMATS:
        if r.get(f'fmt_{f}', 0) == 1:
            return f
    if 'fmt_Credit Card' not in r and all(r.get(f'fmt_{f}', 0) == 0 for f in FORMATS if f != 'Credit Card'):
        return 'Credit Card'          # the only format without its own one-hot column
    return 'Unknown'


def _tx_id(row):
    return f'TX-{int(row):07d}'


def _acc_id(acct):
    return f'ACC-{int(acct)}'


def _parse_ref(ref, prefix):
    s = str(ref).strip().upper().replace(' ', '')
    if s.startswith(prefix + '-'):
        s = s[len(prefix) + 1:]
    return int(s) if s.isdigit() else None


def _level_name(code):
    return LEVELS[code] if 0 <= code < len(LEVELS) else None


def _need_index():
    if TXN.ready():
        return None
    st = TXN.status()
    code = 503 if st['state'] in ('building', 'idle') else 409
    return jsonify(dict(error=st['message'] or 'Transaction index is still building.', index=st)), code


def _txn_payload(r, row):
    a = TXN.a
    amount = float(r.get('Amount Paid', 0.0))
    ratio = float(r.get('amount_vs_account_median', 0.0) or 0.0)
    dow = int(r.get('DayOfWeek', -1)) if isinstance(r.get('DayOfWeek'), float) else -1
    level_code = int(a['risk_level'][row])
    alert_id = TXN.row_alert.get(int(row))
    flags = dict(large_amount=bool(r.get('is_amount_outlier_iqr', 0) == 1),
                 extreme_amount=bool(r.get('is_amount_outlier_zscore', 0) == 1),
                 personal_outlier=bool(r.get('is_account_level_outlier', 0) == 1),
                 high_velocity=bool(r.get('is_high_velocity', 0) == 1))
    fmt = _fmt_from_row(r)
    return dict(
        row=int(row), tx_id=_tx_id(row),
        day=int(r.get('Day', 0)), hour=int(r.get('Hour', 0)),
        weekday=WEEKDAYS[dow] if 0 <= dow < 7 else None, day_of_week=dow,
        is_weekend=bool(r.get('IsWeekend', 0) == 1),
        sender=dict(account=int(r['Sender_Account_Encoded']), account_id=_acc_id(r['Sender_Account_Encoded']),
                    bank=int(r['Sender Bank ID']) if 'Sender Bank ID' in r else None),
        receiver=dict(account=int(r['Receiver_Account_Encoded']), account_id=_acc_id(r['Receiver_Account_Encoded']),
                      bank=int(r['Receiver Bank ID']) if 'Receiver Bank ID' in r else None),
        amount_paid=amount, pay_currency=_cur(r.get('Payment_Currency_Encoded')),
        amount_received=float(r.get('Amount Received', amount)), recv_currency=_cur(r.get('Receiving_Currency_Encoded')),
        payment_format=fmt,
        cross_bank=bool(r.get('is_cross_bank', 0) == 1), self_transfer=bool(r.get('is_self_transfer', 0) == 1),
        currency_mismatch=bool(r.get('is_currency_mismatch', 0) == 1),
        ratio_vs_usual=round(ratio, 4),
        usual_amount=round(amount / ratio, 2) if ratio > 0 else None,
        txn_hour=int(r.get('txn_count_per_sender_hour', 0)), txn_day=int(r.get('txn_count_per_sender_day', 0)),
        flags=flags, outlier_score=int(r.get('outlier_score', sum(flags.values()))),
        label=int(r.get('Is Laundering', 0)),
        risk=dict(available=TXN.risk_available,
                  score=None if np.isnan(a['risk_score'][row]) else round(float(a['risk_score'][row]), 2),
                  level=_level_name(level_code),
                  xgb_stored=None if np.isnan(a['xgb_stored'][row]) else round(float(a['xgb_stored'][row]), 6),
                  source='Risk Engine · results/final_risk_scores.csv' if TXN.risk_available else TXN.risk_note),
        alert=dict(alert_id=alert_id, status=WORKFLOW.status(alert_id) if alert_id else None,
                   ambiguous=alert_id in TXN.alert_ambiguous) if alert_id else None,
    )


def _model_row(r):
    """The 30 production-model features, rebuilt exactly from a cleaned row
    (formulas verified against the model's own saved feature matrix)."""
    paid, recv = float(r['Amount Paid']), float(r['Amount Received'])
    f = {c: float(r[c]) for c in MODEL_DIRECT if c not in ('Amount Paid', 'Amount Received')}
    f['Amount_Paid_Log'] = float(np.log1p(paid))
    f['Amount_Received_Log'] = float(np.log1p(recv))
    f['Amount_Diff_Log'] = float(np.log1p(abs(paid - recv)))
    f['is_outlier'] = float(r['is_outlier']) if 'is_outlier' in r else (1.0 if f['outlier_score'] > 0 else 0.0)
    fmt = _fmt_from_row(r)
    for name in FORMATS:
        f[f'fmt_{name}'] = 1.0 if fmt == name else 0.0
    return f


# ================================================================
# ANALYST WORKFLOW + AUDIT TRAIL
# ================================================================
AUDIT_PATH = os.path.join(BASE, 'results', 'analyst_audit_log.jsonl')
ACTIONS = {
    'CONFIRM_FRAUD': 'CONFIRMED_FRAUD',
    'DISMISS': 'DISMISSED',
    'ESCALATE': 'ESCALATED',
    'FILE_SAR': 'SAR_FILED',
    'REOPEN': 'PENDING_REVIEW',
}
STATUS_ORDER = ['PENDING_REVIEW', 'ESCALATED', 'CONFIRMED_FRAUD', 'SAR_FILED', 'DISMISSED']
STATUS_LABEL = {'PENDING_REVIEW': 'Pending review', 'ESCALATED': 'Escalated', 'CONFIRMED_FRAUD': 'Confirmed fraud',
                'SAR_FILED': 'SAR filed', 'DISMISSED': 'Dismissed'}
SAR_READY = ('CONFIRMED_FRAUD', 'ESCALATED')   # a SAR is filed only after one of these


class Workflow:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.entries = []
        self.latest = {}
        self._load()

    def _load(self):
        if not os.path.isfile(self.path):
            return
        with open(self.path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                self.entries.append(e)
                self.latest[e['alert_id']] = e

    def initial_status(self, alert_id):
        al = base.alerts_df
        if al is not None and 'alert_status' in al.columns:
            hit = _ALERT_POS.get(alert_id)
            if hit is not None:
                return str(al['alert_status'].iat[hit])
        return 'PENDING_REVIEW'

    def status(self, alert_id):
        e = self.latest.get(alert_id)
        return e['status_after'] if e else self.initial_status(alert_id)

    def history(self, alert_id=None, limit=200):
        items = [e for e in self.entries if alert_id is None or e['alert_id'] == alert_id]
        return list(reversed(items))[:limit]

    def record(self, alert_id, action, analyst, note, tx_id):
        with self.lock:
            before = self.status(alert_id)
            e = dict(ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                     alert_id=alert_id, tx_id=tx_id, action=action,
                     status_before=before, status_after=ACTIONS[action],
                     analyst=analyst, note=note)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + '\n')
            self.entries.append(e)
            self.latest[alert_id] = e
            return e


_ALERT_POS = {}
_INITIAL_COUNTS = Counter()
if base.alerts_df is not None and not base.alerts_df.empty and 'alert_id' in base.alerts_df.columns:
    _ALERT_POS = {aid: i for i, aid in enumerate(base.alerts_df['alert_id'].astype(str).tolist())}
    _INITIAL_COUNTS = (Counter(base.alerts_df['alert_status'].astype(str)) if 'alert_status' in base.alerts_df.columns
                       else Counter({'PENDING_REVIEW': len(_ALERT_POS)}))
WORKFLOW = Workflow(AUDIT_PATH)


def _alert_record(alert_id):
    i = _ALERT_POS.get(alert_id)
    if i is None:
        return None
    r = base.alerts_df.iloc[i]
    return dict(alert_id=alert_id, risk_level=str(r.get('risk_level', '')),
                risk_score=round(float(r.get('risk_score_100', 0) or 0), 2),
                xgb_score=round(float(r.get('xgb_score', 0) or 0), 6),
                cnn_score=round(float(r.get('cnn_score', 0) or 0), 6),
                composite=round(float(r.get('composite_score', 0) or 0), 4),
                label=int(r.get('Is Laundering', 0)) if 'Is Laundering' in r else None,
                queue_status=str(r.get('alert_status', 'PENDING_REVIEW')),
                status=WORKFLOW.status(alert_id))


# ================================================================
# API — INVESTIGATION WORKBENCH
# ================================================================
@app.route('/api/wb/status')
def api_wb_status():
    return jsonify(TXN.status())


@app.route('/api/wb/queue_meta')
def api_wb_queue_meta():
    ids = [s for s in request.args.get('ids', '').split(',') if s][:200]
    out = {}
    for aid in ids:
        row = TXN.alert_row.get(aid) if TXN.ready() else None
        out[aid] = dict(status=WORKFLOW.status(aid),
                        tx_id=_tx_id(row) if row is not None else None)
    return jsonify(out)


@app.route('/api/wb/cases')
def api_wb_cases():
    want = request.args.get('status', '')
    rows = []
    for aid, e in WORKFLOW.latest.items():
        if want and e['status_after'] != want:
            continue
        rec = _alert_record(aid) or dict(alert_id=aid)
        rec.update(last_action=e)
        rows.append(rec)
    rows.sort(key=lambda r: r['last_action']['ts'], reverse=True)
    return jsonify(dict(cases=rows, total=len(rows)))


@app.route('/api/wb/alert/<alert_id>')
def api_wb_alert(alert_id):
    alert_id = alert_id.strip().upper()
    rec = _alert_record(alert_id)
    if rec is None:
        return jsonify(error=f'{alert_id} is not in the alert queue.'), 404
    out = dict(alert=rec, txn=None, txn_note='', audit=WORKFLOW.history(alert_id))
    if not TXN.ready():
        out['txn_note'] = TXN.status()['message'] or 'Transaction index is still building.'
        out['index'] = TXN.status()
        return jsonify(out)
    row = TXN.alert_row.get(alert_id)
    if row is None:
        out['txn_note'] = ('This alert could not be matched to a single transaction row '
                           '(Risk Engine scores unavailable or not aligned).')
        return jsonify(out)
    out['txn'] = _txn_payload(TXN.fetch([row])[0], row)
    return jsonify(out)


@app.route('/api/wb/txn/<ref>')
def api_wb_txn(ref):
    err = _need_index()
    if err:
        return err
    s = str(ref).strip().upper()
    if s.startswith('AML-'):
        row = TXN.alert_row.get(s)
        if row is None:
            return jsonify(error=f'{s} has no matched transaction.'), 404
    else:
        row = _parse_ref(s, 'TX')
        if row is None:
            return jsonify(error='Enter a Transaction ID like TX-0001234 or 1234.'), 400
    if not 0 <= row < TXN.n:
        return jsonify(error=f'Transaction {_tx_id(row)} does not exist '
                             f'(valid: TX-0000000 … {_tx_id(TXN.n - 1)}).'), 404
    txn = _txn_payload(TXN.fetch([row])[0], row)
    return jsonify(dict(txn=txn, audit=WORKFLOW.history(txn['alert']['alert_id']) if txn['alert'] else []))


class _ApiError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


@app.route('/api/wb/explain/<int:row>')
def api_wb_explain(row):
    err = _need_index()
    if err:
        return err
    try:
        return jsonify(_explain_row(row))
    except _ApiError as ex:
        return jsonify(error=str(ex)), ex.code


def _explain_row(row):
    """Exact TreeSHAP explanation of one transaction. Shared by the
    'Why flagged?' tab and the SAR report."""
    if not 0 <= row < TXN.n:
        raise _ApiError('Unknown transaction.', 404)
    if model_xgb is None:
        raise _ApiError('XGBoost model is not loaded on the server.', 500)
    if not TXN.model_ready:
        raise _ApiError('cleaned_transactions.csv lacks model columns: ' + ', '.join(TXN.model_missing), 409)
    r = TXN.fetch([row])[0]
    feats = _model_row(r)
    actual_day = feats['Day']
    feats['Day'] = DAY_NEUTRAL
    order = XGB_FEATURES if XGB_FEATURES else list(feats.keys())
    raw = pd.DataFrame([[feats.get(f, 0.0) for f in order]], columns=order)
    scaled = _scale_frame(raw)
    prob = float(model_xgb.predict_proba(scaled)[0][1])
    pct = _portal_percentile(prob)
    level = _portal_level(pct)
    txn = _txn_payload(r, row)
    ctx = dict(payment_format=txn['payment_format'], amount=txn['amount_paid'],
               send_currency=txn['pay_currency'], receive_currency=txn['recv_currency'],
               ratio=txn['ratio_vs_usual'], txn_hour=txn['txn_hour'], txn_day=txn['txn_day'],
               self_transfer=txn['self_transfer'], cross_bank=txn['cross_bank'],
               sender_bank=txn['sender']['bank'], receiver_bank=txn['receiver']['bank'],
               currency_mismatch=txn['currency_mismatch'], hour=txn['hour'],
               weekday=max(0, txn['day_of_week']), flags=txn['flags'],
               outlier_score=txn['outlier_score'])
    try:
        sv, base_value = _portal_shap(scaled, order)
    except Exception as ex:
        raise _ApiError(f'SHAP computation failed: {ex}', 500)
    shap_map = dict(zip(order, map(float, sv)))
    reasons = _portal_reasons(shap_map, ctx)
    feats['Day'] = actual_day
    top = sorted(order, key=lambda f: abs(shap_map[f]), reverse=True)[:12]
    return dict(
        row=row, tx_id=_tx_id(row),
        model=dict(probability=round(prob, 6), percentile=round(pct, 2), level=level,
                   decision=DECISIONS[level][0]),
        stored=txn['risk'],
        reasons=reasons,
        features=[dict(feature=f, value=round(feats.get(f, 0.0), 6), shap=round(shap_map[f], 4)) for f in top],
        base_value=round(float(base_value), 6),
        notes=[f"Exact TreeSHAP of the production XGBoost model on this transaction's own features "
               f"(rebuilt from data/cleaned_transactions.csv, scaled as in training).",
               f"Day-of-month is held at the training median (5) for scoring; this transaction's actual day is "
               f"{int(actual_day)}. In the IBM dataset days 11–18 are almost all laundering, so Day is a data artefact."])


def _graph_tags(acct):
    """Community / mule tags from the Graph Network Analysis notebook, as
    loaded by app.py (empty if precompute_transactions.py hasn't been run)."""
    community, mule = getattr(base, 'ACCOUNT_FLAGS', {}).get(str(int(acct)), [None, 0])
    stats = getattr(base, 'community_stats', {}).get(community, {}) if community is not None else {}
    return dict(community=community, community_suspicious=bool(stats.get('suspicious', False)),
                community_fraud_rate=stats.get('fraud_rate'), mule=bool(mule))


def _wb_account_summary(acct, sent=None, recv=None):
    a = TXN.a
    sent = TXN.rows_for(acct, 'sender') if sent is None else sent
    recv = TXN.rows_for(acct, 'receiver') if recv is None else recv

    def by_cur(idx, amt, cur):
        if not len(idx):
            return []
        s = pd.Series(a[amt][idx]).groupby(a[cur][idx]).agg(['sum', 'count'])
        s = s.sort_values('count', ascending=False)
        return [dict(currency=_cur(c), total=round(float(v['sum']), 2), count=int(v['count']))
                for c, v in s.iterrows()]

    both = np.union1d(sent, recv).astype(np.int64)
    lv = a['risk_level'][both]
    tk = a['time_key'][both]
    cps = np.union1d(a['receiver'][sent], a['sender'][recv])
    cps = cps[cps != acct]
    return dict(
        account=acct, account_id=_acc_id(acct),
        sent=int(len(sent)), received=int(len(recv)), total=int(len(both)),
        sent_by_currency=by_cur(sent, 'paid', 'pay_cur'),
        received_by_currency=by_cur(recv, 'received', 'recv_cur'),
        counterparties=int(len(cps)),
        first_seen=dict(day=int(tk.min() // 24), hour=int(tk.min() % 24)) if len(tk) else None,
        last_seen=dict(day=int(tk.max() // 24), hour=int(tk.max() % 24)) if len(tk) else None,
        alert_txns=int((lv >= LEVEL_CODE['HIGH']).sum()) if TXN.risk_available else None,
        max_level=_level_name(int(lv.max())) if TXN.risk_available and len(lv) else None,
        labelled_laundering=int(a['label'][both].sum()),
        graph=_graph_tags(acct),
    )


@app.route('/api/wb/account/<ref>')
def api_wb_account(ref):
    err = _need_index()
    if err:
        return err
    acct = _parse_ref(ref, 'ACC')
    if acct is None:
        return jsonify(error='Enter an Account ID like ACC-743 or 743.'), 400
    a = TXN.a
    sent = TXN.rows_for(acct, 'sender')
    recv = TXN.rows_for(acct, 'receiver')
    if not len(sent) and not len(recv):
        return jsonify(error=f'{_acc_id(acct)} has no transactions in the dataset.'), 404
    role = request.args.get('role', 'all')
    rows = np.union1d(sent, recv) if role == 'all' else (sent if role == 'sent' else recv)
    rows = np.asarray(rows, dtype=np.int64)
    order = np.lexsort((-rows, -a['time_key'][rows]))
    rows = rows[order]
    page = max(1, int(sf(request.args.get('page', 1), 0)))
    per = min(100, max(5, int(sf(request.args.get('per_page', 25), 0))))
    chunk = rows[(page - 1) * per: page * per]
    summary = _wb_account_summary(acct, sent, recv)
    items = []
    for r, full in zip(chunk.tolist(), TXN.fetch(chunk)):
        t = _txn_payload(full, r)
        t['direction'] = ('self' if t['sender']['account'] == t['receiver']['account'] else
                          'out' if t['sender']['account'] == acct else 'in')
        items.append(t)
    return jsonify(dict(summary=summary, role=role, rows=items, total=int(len(rows)), page=page,
                        pages=max(1, (len(rows) + per - 1) // per), per_page=per))


@app.route('/api/wb/network/<ref>')
def api_wb_network(ref):
    err = _need_index()
    if err:
        return err
    acct = _parse_ref(ref, 'ACC')
    if acct is None:
        return jsonify(error='Enter an Account ID like ACC-743 or 743.'), 400
    depth = 2 if request.args.get('depth') == '2' else 1
    limit = min(40, max(5, int(sf(request.args.get('limit', 20), 0))))
    fan = 5
    a = TXN.a

    def edges_of(center, top_k):
        """Real transactions between `center` and its `top_k` busiest
        counterparties, aggregated per direction. Returns (edges, total_cps)."""
        out_rows = TXN.rows_for(center, 'sender').astype(np.int64)
        in_rows = TXN.rows_for(center, 'receiver').astype(np.int64)
        rows = np.concatenate([out_rows, in_rows])
        if not len(rows):
            return [], 0
        cp = np.concatenate([a['receiver'][out_rows], a['sender'][in_rows]]).astype(np.int64)
        direction = np.concatenate([np.zeros(len(out_rows), np.int8), np.ones(len(in_rows), np.int8)])
        keep = cp != center
        rows, cp, direction = rows[keep], cp[keep], direction[keep]
        if not len(rows):
            return [], 0
        uniq, counts = np.unique(cp, return_counts=True)
        top = uniq[np.argsort(-counts, kind='stable')[:top_k]]
        sel = np.isin(cp, top)
        df = pd.DataFrame(dict(cp=cp[sel], dir=direction[sel], cur=a['pay_cur'][rows[sel]],
                               amt=a['paid'][rows[sel]], lab=a['label'][rows[sel]],
                               lvl=a['risk_level'][rows[sel]]))
        g = df.groupby(['cp', 'dir']).agg(count=('amt', 'size'), lab=('lab', 'sum'), lvl=('lvl', 'max'))
        cur = df.groupby(['cp', 'dir', 'cur'])['amt'].sum()
        recs = []
        for (c, d), v in g.iterrows():
            totals = cur.loc[(c, d)].sort_values(ascending=False).head(3)
            recs.append(dict(cp=int(c), direction='out' if d == 0 else 'in', count=int(v['count']),
                             laundering=int(v['lab']), max_level=int(v['lvl']),
                             by_currency=[dict(currency=_cur(k), total=round(float(t), 2))
                                          for k, t in totals.items()]))
        recs.sort(key=lambda e: e['count'], reverse=True)
        return recs, int(len(uniq))

    def node_stats(acc):
        rows = np.union1d(TXN.rows_for(acc, 'sender'), TXN.rows_for(acc, 'receiver')).astype(np.int64)
        lv = a['risk_level'][rows]
        return dict(txns=int(len(rows)),
                    alert_txns=int((lv >= LEVEL_CODE['HIGH']).sum()) if TXN.risk_available else None,
                    laundering=int(a['label'][rows].sum()),
                    max_level=_level_name(int(lv.max())) if TXN.risk_available and len(rows) else None)

    nodes, edges = {}, []

    def add_node(acc, hop):
        if acc not in nodes:
            nodes[acc] = dict(id=_acc_id(acc), account=acc, hop=hop, **node_stats(acc))

    def add_edge(src_center, e):
        s, t = (src_center, e['cp']) if e['direction'] == 'out' else (e['cp'], src_center)
        edges.append(dict(id=f'{_acc_id(s)}>{_acc_id(t)}', source=_acc_id(s), target=_acc_id(t),
                          count=e['count'], laundering=e['laundering'],
                          max_level=_level_name(e['max_level']), by_currency=e['by_currency']))

    add_node(acct, 0)
    first, cp_total = edges_of(acct, limit)
    shown = []
    for e in first:
        if e['cp'] not in shown:
            shown.append(e['cp'])
        add_node(e['cp'], 1)
        add_edge(acct, e)
    second_total = 0
    if depth == 2:
        for cp in shown[:8]:
            sec, n_cp = edges_of(cp, fan + 1)
            sec = [e for e in sec if e['cp'] != acct]
            second_total += n_cp - (1 if n_cp else 0)
            picked = []
            for e in sec:
                if e['cp'] not in picked:
                    if len(picked) >= fan:
                        continue
                    picked.append(e['cp'])
                add_node(e['cp'], 2)
                add_edge(cp, e)
    seen, uniq = set(), []
    for e in edges:
        if e['id'] not in seen:
            seen.add(e['id'])
            uniq.append(e)
    return jsonify(dict(center=_acc_id(acct), depth=depth, nodes=list(nodes.values()), edges=uniq,
                        counterparties_total=cp_total, counterparties_shown=len(shown),
                        second_hop_total=second_total if depth == 2 else None,
                        source='data/cleaned_transactions.csv — every edge is real transactions '
                               'between the two accounts, aggregated by direction'))


@app.route('/api/wb/action', methods=['POST'])
def api_wb_action():
    d = request.get_json(silent=True) or {}
    alert_id = str(d.get('alert_id', '')).strip().upper()
    action = str(d.get('action', '')).strip().upper()
    analyst = str(d.get('analyst', '')).strip()[:60]
    note = str(d.get('note', '')).strip()[:1000]
    if alert_id not in _ALERT_POS:
        return jsonify(error=f'{alert_id or "(empty)"} is not in the alert queue.'), 404
    if action not in ACTIONS:
        return jsonify(error=f'Unknown action. Use one of {sorted(ACTIONS)}.'), 400
    if len(analyst) < 2:
        return jsonify(error='Enter the analyst name (at least 2 characters).'), 400
    if len(note) < 5:
        return jsonify(error='Add a note explaining the decision (at least 5 characters).'), 400
    current = WORKFLOW.status(alert_id)
    label = STATUS_LABEL.get(current, current)
    if action == 'FILE_SAR' and current not in SAR_READY:
        return jsonify(error=f'Confirm fraud or escalate this alert before filing a SAR (current status: {label}).'), 409
    if ACTIONS[action] == current:
        return jsonify(error=f'This alert is already marked "{label}".'), 409
    row = TXN.alert_row.get(alert_id) if TXN.ready() else None
    entry = WORKFLOW.record(alert_id, action, analyst, note, _tx_id(row) if row is not None else None)
    return jsonify(dict(ok=True, entry=entry, status=entry['status_after'],
                        audit=WORKFLOW.history(alert_id)))


@app.route('/api/wb/audit')
def api_wb_audit():
    alert_id = request.args.get('alert_id', '').strip().upper() or None
    limit = min(500, max(1, int(sf(request.args.get('limit', 50), 0))))
    return jsonify(dict(entries=WORKFLOW.history(alert_id, limit), path='results/analyst_audit_log.jsonl'))


@app.route('/api/wb/queue_stats')
def api_wb_queue_stats():
    """Live alert counts by analyst decision (starting status from alerts.csv,
    adjusted by every decision in the audit log)."""
    counts = Counter(_INITIAL_COUNTS)
    with WORKFLOW.lock:
        latest = list(WORKFLOW.latest.items())
    for aid, e in latest:
        if aid in _ALERT_POS:
            counts[WORKFLOW.initial_status(aid)] -= 1
            counts[e['status_after']] += 1
    return jsonify(dict(total=len(_ALERT_POS), counts={s: int(counts.get(s, 0)) for s in STATUS_ORDER},
                        worked=sum(1 for aid, _ in latest if aid in _ALERT_POS)))


# ================================================================
# API — SAR REPORT (Suspicious Activity Report, auto-drafted)
# ----------------------------------------------------------------
# Assembled only from the project's own data and models: the alert,
# its transaction, the exact SHAP explanation, both parties' account
# activity, the Graph notebook's community/mule tags and the analyst
# audit trail. It is a draft for an analyst to review — filing it is a
# separate FILE_SAR action (/api/wb/action) that goes into the audit log.
# ================================================================
def _money(v, cur):
    return f'{float(v):,.2f} {cur}'


def _party_line(p, summary):
    g = summary['graph']
    tags = [f"bank {p['bank']}"]
    if g['community'] is not None:
        tags.append(f"community {g['community']}" + (' (suspicious)' if g['community_suspicious'] else ''))
    if g['mule']:
        tags.append('flagged mule account')
    return f"{p['account_id']} · " + ' · '.join(tags)


def _activity_line(s):
    parts = [f"{s['total']:,} transactions ({s['sent']:,} sent, {s['received']:,} received)",
             f"{s['counterparties']:,} counterparties"]
    if s['alert_txns'] is not None:
        parts.append(f"{s['alert_txns']:,} at alert level")
    parts.append(f"{s['labelled_laundering']:,} labelled laundering in the dataset")
    if s['first_seen'] and s['last_seen']:
        parts.append(f"active Day {s['first_seen']['day']} to Day {s['last_seen']['day']}")
    return ', '.join(parts)


@app.route('/api/wb/sar/<alert_id>')
def api_wb_sar(alert_id):
    alert_id = alert_id.strip().upper()
    rec = _alert_record(alert_id)
    if rec is None:
        return jsonify(error=f'{alert_id} is not in the alert queue.'), 404
    err = _need_index()
    if err:
        return err
    row = TXN.alert_row.get(alert_id)
    if row is None:
        return jsonify(error='This alert could not be matched to a transaction, so a SAR cannot be drafted.'), 409

    t = _txn_payload(TXN.fetch([row])[0], row)
    try:
        why, why_note = _explain_row(row), None
    except _ApiError as ex:
        why, why_note = None, str(ex)
    snd = _wb_account_summary(t['sender']['account'])
    rcv = snd if t['self_transfer'] else _wb_account_summary(t['receiver']['account'])
    audit = WORKFLOW.history(alert_id)
    status = rec['status']
    now = datetime.datetime.now(datetime.timezone.utc)
    when = f"Day {t['day']}, {t['hour']:02d}:00" + (f" ({t['weekday']})" if t['weekday'] else '')
    route = ('a self-transfer' if t['self_transfer'] else
             'a cross-bank transfer' if t['cross_bank'] else 'a same-bank transfer')
    paid = _money(t['amount_paid'], t['pay_currency'])
    received = _money(t['amount_received'], t['recv_currency'])

    red_flags = [k for k, on in (('amount above the dataset\'s large-amount fence', t['flags']['large_amount']),
                                 ('extreme amount (z-score)', t['flags']['extreme_amount']),
                                 ('5× or more the sender\'s usual amount', t['flags']['personal_outlier']),
                                 ('high velocity (10+ transfers in the hour)', t['flags']['high_velocity']),
                                 ('currency converted between payment and receipt', t['currency_mismatch']),
                                 ('cross-bank transfer', t['cross_bank']),
                                 ('self-transfer', t['self_transfer'])) if on]
    raises = [r for r in (why['reasons'] if why else []) if r['impact'] > 0.005][:4]
    lowers = [r for r in (why['reasons'] if why else []) if r['impact'] < -0.005][:2]

    sections = [
        dict(title='1. Filing information', rows=[
            ('Alert ID', alert_id), ('Transaction ID', t['tx_id']),
            ('Report status', 'SAR filed' if status == 'SAR_FILED' else 'Draft — not filed'),
            ('Case status', STATUS_LABEL.get(status, status)),
            ('Generated', now.strftime('%Y-%m-%d %H:%M UTC'))]),
        dict(title='2. Subjects', rows=[
            ('Sender (originator)', _party_line(t['sender'], snd)),
            ('Receiver (beneficiary)', _party_line(t['receiver'], rcv))]),
        dict(title='3. Suspicious activity', rows=[
            ('Date / time', when), ('Amount paid', paid), ('Amount received', received),
            ('Payment method', t['payment_format']), ('Route', route.capitalize()),
            ('Sender velocity', f"{t['txn_hour']} transfers this hour, {t['txn_day']} today"),
            ('Vs sender\'s usual', f"{t['ratio_vs_usual']:.2f}×"),
            ('Red flags', ', '.join(red_flags) if red_flags else 'none of the preprocessing flags')]),
        dict(title='4. Why it was flagged', rows=[
            ('Risk Engine', f"{rec['risk_score']:.2f} / 100 · {rec['risk_level']}"),
            ('Production XGBoost', (f"{why['model']['percentile']:.1f} percentile · fraud probability "
                                    f"{why['model']['probability'] * 100:.2f}% · {why['model']['level']}")
             if why else f'unavailable ({why_note})'),
            ('Stored model scores', f"XGB {rec['xgb_score']:.6f} · CNN {rec['cnn_score']:.6f} · composite {rec['composite']:.4f}")],
             factors=dict(raises=raises, lowers=lowers)),
        dict(title='5. Account activity', rows=[
            (f"Sender {snd['account_id']}", _activity_line(snd))] +
            ([] if t['self_transfer'] else [(f"Receiver {rcv['account_id']}", _activity_line(rcv))])),
        dict(title='6. Analyst review', audit=[
            dict(ts=e['ts'], analyst=e['analyst'], action=e['action'], status_after=e['status_after'], note=e['note'])
            for e in audit]),
    ]

    factor_text = '; '.join(f"{r['title']} — {r['detail']}" for r in raises)
    method = t['payment_format'] if t['payment_format'] != 'Unknown' else 'an unrecorded payment method'
    done = {'CONFIRM_FRAUD': 'confirmed as fraud', 'DISMISS': 'dismissed', 'ESCALATE': 'escalated',
            'FILE_SAR': 'SAR filed', 'REOPEN': 'reopened'}
    graph_bits = []
    for label, s in (('sender', snd), ('receiver', rcv)):
        g = s['graph']
        if g['mule']:
            graph_bits.append(f'the {label} is a flagged mule account')
        if g['community_suspicious']:
            graph_bits.append(f"the {label} belongs to suspicious community {g['community']}")
    narrative = (
        f"On {when}, account {t['sender']['account_id']} (bank {t['sender']['bank']}) sent {paid} by "
        f"{method} to account {t['receiver']['account_id']} (bank {t['receiver']['bank']}), "
        f"{route}; {received} was received. The Risk Engine scored the transaction {rec['risk_score']:.2f}/100 "
        f"({rec['risk_level']}) and raised alert {alert_id}. "
        + (f"The production XGBoost model places it in the {why['model']['percentile']:.1f} percentile of scored "
           f"transactions (fraud probability {why['model']['probability'] * 100:.2f}%). " if why else '')
        + (f"The factors that raised the risk most: {factor_text}. " if factor_text else '')
        + (f"Red flags from preprocessing: {', '.join(red_flags)}. " if red_flags else '')
        + f"Across the dataset the sender made {snd['total']:,} transactions with {snd['counterparties']:,} "
          f"counterparties, {snd['labelled_laundering']:,} of them labelled laundering. "
        + (f"Network analysis: {'; '.join(graph_bits)}. " if graph_bits else '')
        + (f"Latest analyst action: {done.get(audit[0]['action'], audit[0]['action'])} by {audit[0]['analyst']} — "
           f"\"{audit[0]['note']}\"." if audit else 'No analyst has reviewed this alert yet.'))

    lines = ['SUSPICIOUS ACTIVITY REPORT — ' + ('FILED' if status == 'SAR_FILED' else 'DRAFT'),
             'Generated by FinShield AI from project data and models', '=' * 64]
    for s in sections:
        lines += ['', s['title'].upper()]
        lines += [f'  {k:<24} {v}' for k, v in s.get('rows', [])]
        if s.get('factors'):
            for heading, key, sign in (('Factors raising risk:', 'raises', '+'), ('Factors lowering risk:', 'lowers', '-')):
                lines.append('  ' + heading)
                lines += ([f"    {sign} {r['title']}: {r['detail']} ({r['impact']:+.2f})" for r in s['factors'][key]]
                          or ['    (none)'])
        if 'audit' in s:
            lines += [f"  {e['ts']}  {e['action']:<14} {e['analyst']}: {e['note']}" for e in s['audit']] \
                or ['  No analyst actions recorded yet.']
    disclaimer = ('Auto-drafted from the IBM AML dataset, the Risk Engine and the production XGBoost model. '
                  'It must be reviewed and completed by an analyst before it is filed with any authority.')
    lines += ['', '7. NARRATIVE', narrative, '', '=' * 64, disclaimer]

    return jsonify(dict(alert_id=alert_id, tx_id=t['tx_id'], status=status,
                        filed=status == 'SAR_FILED', can_file=status in SAR_READY,
                        generated_at=now.isoformat(timespec='seconds'),
                        sections=[dict(s, rows=[dict(label=k, value=v) for k, v in s.get('rows', [])])
                                  for s in sections],
                        narrative=narrative, text='\n'.join(lines), disclaimer=disclaimer))


# ================================================================
# FRONT-END INTEGRATION
# ----------------------------------------------------------------
# new.html holds three marked sections (STYLE / NAV / PAGES). They are
# injected into the page app.py serves; new.js is appended after the
# existing dashboard script. Two known front-end defects are corrected on
# the fly (files on disk are not modified):
#   • a stray "};" in the ML Model Comparison loader — a syntax error that
#     stops the whole dashboard script;
#   • a second copy of the Link Security page nested inside the sidebar.
# ================================================================
NEW_HTML = os.path.join(HERE, 'new.html')
NEW_JS = os.path.join(HERE, 'new.js')


def _new_html_parts():
    with open(NEW_HTML, encoding='utf-8') as fh:
        text = fh.read()
    parts = {}
    for name in ('STYLE', 'NAV', 'PAGES'):
        m = re.search(rf'<!--\s*NEW:{name}\s*-->(.*?)<!--\s*/NEW:{name}\s*-->', text, re.S)
        parts[name] = m.group(1).strip() if m else ''
    return parts


def _check_new_html():
    """Say so loudly if new.html can't be injected — otherwise the two tabs
    just silently don't appear."""
    if not os.path.isfile(NEW_HTML):
        print(f'  ✗ {NEW_HTML} not found — User Portal and Investigation Workbench tabs will NOT appear')
        return
    missing = [n for n, v in _new_html_parts().items() if not v]
    if missing:
        print(f"  ✗ new.html is missing its NEW:{'/NEW:'.join(missing)} marker section(s) — "
              f"User Portal and Investigation Workbench tabs will NOT appear. "
              f"Keep the <!-- NEW:STYLE -->, <!-- NEW:NAV --> and <!-- NEW:PAGES --> comments (and their /NEW: closers).")
    else:
        print('  ✓ new.html ready — User Portal and Investigation Workbench tabs will be injected')


def _matching_div_end(html, start):
    depth = 0
    for m in re.finditer(r'<div\b|</div\s*>', html[start:]):
        depth += 1 if m.group(0).startswith('<div') else -1
        if depth == 0:
            return start + m.end()
    return -1


def _fix_known_page_issues(html):
    nav = html.find('class="nav-scroll"')
    bottom = html.find('class="sidebar-bottom"', nav)
    if nav >= 0 and bottom > nav and html.count('id="pg-linksecurity"') > 1:
        i = html.find('<div class="page" id="pg-linksecurity"', nav, bottom)
        if i >= 0:
            j = _matching_div_end(html, i)
            if j > 0:
                html = html[:i] + html[j:]
    return html


_JS_ML_FIX = re.compile(r"(\}\);[ \t]*\r?\n)\};[ \t]*\r?\n(\s*makeChart\('chart-ml-recall-prec')")


def _fix_known_js_issues(text):
    return _JS_ML_FIX.sub(r'\1\2', text)


def _inject_new_ui(html):
    if 'id="pg-workbench"' in html or not os.path.isfile(NEW_HTML):
        return html
    parts = _new_html_parts()
    if parts['STYLE']:
        html = html.replace('</head>', parts['STYLE'] + '\n</head>', 1)
    if parts['NAV']:
        m = re.search(r'<div class="nav-item[^"]*"\s+data-page="overview"[^>]*>.*?</div>', html, re.S)
        if m:
            html = html[:m.end()] + '\n' + parts['NAV'] + html[m.end():]
    if parts['PAGES']:
        m = re.search(r'<div class="content"[^>]*id="content"[^>]*>', html)
        if m:
            html = html[:m.end()] + '\n' + parts['PAGES'] + html[m.end():]
        else:
            html = html.replace('</body>', parts['PAGES'] + '\n</body>', 1)
    k = html.rfind('</body>')
    tag = '<script src="/new.js"></script>\n'
    return html[:k] + tag + html[k:] if k >= 0 else html + tag


_orig_index = app.view_functions['index']


def index_with_new_features():
    resp = _orig_index()
    if isinstance(resp, tuple) or getattr(resp, 'mimetype', '') != 'text/html':
        return resp
    html = resp.get_data(as_text=True)
    html = _inject_new_ui(_fix_known_page_issues(html))
    resp.set_data(html)
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp


app.view_functions['index'] = index_with_new_features


def _serve_fixed_js(path):
    with open(path, encoding='utf-8') as fh:
        text = _fix_known_js_issues(fh.read())
    return Response(text, mimetype='application/javascript', headers={'Cache-Control': 'no-store'})


@app.route('/static/dashboard_js.js')
def serve_dashboard_js_fixed():
    for p in (os.path.join(HERE, 'static', 'dashboard_js.js'), os.path.join(HERE, 'dashboard_js.js')):
        if os.path.isfile(p):
            return _serve_fixed_js(p)
    return Response('console.error("dashboard_js.js not found");',
                    mimetype='application/javascript', status=404)


_orig_dashboard_js = app.view_functions.get('serve_dashboard_js')


def serve_dashboard_js_patched():
    p = os.path.join(HERE, 'static', 'dashboard.js')
    if os.path.isfile(p):
        return _serve_fixed_js(p)
    if _orig_dashboard_js:
        return _orig_dashboard_js()
    return Response('', status=404)


if _orig_dashboard_js:
    app.view_functions['serve_dashboard_js'] = serve_dashboard_js_patched


@app.route('/new.js')
def serve_new_js():
    resp = send_from_directory(HERE, 'new.js', mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# Start building the transaction index in the background as soon as the
# server loads (the dashboard is usable immediately; workbench lookups
# report progress until the index is ready).
TXN.start()
_check_new_html()


@app.before_request
def _ensure_txn_index():
    if request.path.startswith('/api/wb/'):
        TXN.ensure()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\nFinShield AI (with new.py features) → http://localhost:{port}")
    print("Press CTRL+C to stop\n")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)