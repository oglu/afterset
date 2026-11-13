"""Explore one preselected metric pair; never persist derived data to SQLite.

Run with: uv run --extra analysis python examples/analyze.py --help
Correlations are exploratory: missingness, repeated observations and multiple
comparisons can create misleading patterns. They do not establish causation.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from afterset import connection, db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--predictor', choices=db.METRIC_COLS, required=True)
    parser.add_argument('--outcome', choices=db.METRIC_COLS, required=True)
    parser.add_argument('--lag-days', type=int, choices=range(0, 8), default=0,
                        help='Compare the predictor this many calendar days before the outcome.')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not connection.DB_PATH.is_file():
        parser.error('No database found; configure AFTERSET_DB or run afterset fetch first.')
    rows = db.load_all()
    if not rows:
        parser.error('No daily metrics found.')
    frame = pd.DataFrame(rows).set_index('date')
    frame.index = pd.to_datetime(frame.index)
    # Reindex before shifting: missing dates must not compress calendar lags.
    frame = frame.reindex(pd.date_range(frame.index.min(), frame.index.max()))
    pairs = pd.DataFrame({
        'predictor': pd.to_numeric(frame[args.predictor], errors='coerce').shift(args.lag_days),
        'outcome': pd.to_numeric(frame[args.outcome], errors='coerce'),
    }).dropna()
    if len(pairs) < 10:
        parser.error('Need at least 10 paired numeric observations.')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pairs.to_csv(args.output_dir / 'paired_metrics.csv', index_label='outcome_date')
    pd.DataFrame([{
        'predictor': args.predictor, 'outcome': args.outcome,
        'lag_days': args.lag_days, 'paired_days': len(pairs),
        'pearson': pairs.corr(method='pearson').loc['predictor', 'outcome'],
        # Spearman is Pearson on average ranks; this avoids requiring scipy.
        'spearman': pairs.rank().corr().loc['predictor', 'outcome'],
    }]).to_csv(args.output_dir / 'correlation.csv', index=False)
    fig, ax = plt.subplots()
    sns.scatterplot(data=pairs, x='predictor', y='outcome', ax=ax)
    ax.set(xlabel=f'{args.predictor} ({args.lag_days} days earlier)', ylabel=args.outcome,
           title=f'Exploratory comparison: {len(pairs)} paired days')
    fig.tight_layout()
    fig.savefig(args.output_dir / 'comparison.png', dpi=150)
    plt.close(fig)
    print('Exploratory correlation only; repeated comparisons can produce chance findings.')


if __name__ == '__main__':
    main()
