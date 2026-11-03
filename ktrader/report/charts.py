"""결과 시각화(PNG). 한글 글꼴이 있으면 한글 라벨, 없으면 영문 라벨로 자동 전환."""
from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from ..analysis.factors import FACTOR_SPECS  # noqa: E402
from ..backtest.metrics import drawdown, monthly_returns  # noqa: E402

_KO_FONTS = ["NanumGothic", "Malgun Gothic", "AppleGothic", "Noto Sans CJK KR", "Noto Sans KR"]


def _setup_font() -> bool:
    available = {f.name for f in font_manager.fontManager.ttflist}
    for f in _KO_FONTS:
        if f in available:
            plt.rcParams["font.family"] = f
            plt.rcParams["axes.unicode_minus"] = False
            return True
    return False


KO = _setup_font()


def L(ko: str, en: str) -> str:
    return ko if KO else en


_EN_NAMES = {"모멘텀": "Momentum", "가치": "Value", "멀티팩터": "Multi-factor",
             "RSI 역추세": "RSI mean-reversion", "동일가중 벤치마크": "Equal-weight benchmark",
             "국내 멀티팩터": "KR multi-factor", "글로벌 멀티팩터": "Global multi-factor",
             "글로벌 동일가중": "Global equal-weight"}


def plot_equity(results: dict[str, pd.Series], path: str) -> str:
    """전략별 자산곡선 + 낙폭(drawdown) 2단 차트."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    for i, (name, eq) in enumerate(results.items()):
        label = name if KO else _EN_NAMES.get(name, f"strategy{i + 1}")
        (line,) = ax1.plot(eq.index, eq / eq.iloc[0], label=label, lw=1.4)
        ax2.plot(eq.index, drawdown(eq) * 100, color=line.get_color(), lw=1.0)
    ax1.set_ylabel(L("누적 성장(배)", "Growth (x)"))
    ax1.legend(loc="upper left", frameon=False)
    ax1.grid(alpha=0.3)
    ax2.set_ylabel(L("낙폭(%)", "Drawdown (%)"))
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_contributions(row: pd.Series, title: str, path: str) -> str:
    """한 종목의 팩터 기여도 막대그래프(양수=가점, 음수=감점)."""
    c = {k[len("contrib_"):]: v for k, v in row.items() if k.startswith("contrib_")}
    s = pd.Series(c).sort_values()
    labels = [FACTOR_SPECS[k].label_ko if KO else k for k in s.index]
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.barh(labels, s.values, color=["#c0392b" if v < 0 else "#2e86de" for v in s.values])
    ax.axvline(0, color="#555", lw=0.8)
    ax.set_title(title)
    ax.set_xlabel(L("점수 기여도", "Score contribution"))
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_monthly_heatmap(equity: pd.Series, path: str) -> str:
    m = monthly_returns(equity) * 100
    fig, ax = plt.subplots(figsize=(10, 0.6 * len(m) + 1.2))
    im = ax.imshow(m.values, cmap="RdYlGn", vmin=-10, vmax=10, aspect="auto")
    ax.set_xticks(range(m.shape[1]), [str(c) for c in m.columns])
    ax.set_yticks(range(m.shape[0]), [str(i) for i in m.index])
    for (i, j), v in pd.DataFrame(m.values).stack().items():
        ax.text(j, i, f"{v:.1f}", ha="center", va="center", fontsize=8)
    ax.set_title(L("월별 수익률(%)", "Monthly return (%)"))
    fig.colorbar(im, ax=ax, fraction=0.02)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
