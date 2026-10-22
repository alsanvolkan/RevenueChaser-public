from datetime import date
from RevenueChaser.module_earnings_radar.models import TradeCard


def _bias_label(direction: float) -> str:
    if direction >= 10: return "Bullish"
    if direction <= -10: return "Bearish"
    return "Neutral"


def _vol_bar(score: float, width: int = 8) -> str:
    filled = int(min(score / 100, 1.0) * width)
    return "|" * filled + "." * (width - filled)


def render_dashboard(trade_cards: list[TradeCard], watchlist: set = None, title: str = "REVENUECHASER EARNINGS RADAR") -> None:
    sep = "=" * 135
    print(f"\n  {title}")
    print(f"  {sep}")
    hdr = f"  {'WL':>3} {'Ticker':<7} {'Cluster':<23} {'Bias':<10} {'Dir':>6} {'Vol':>12} {'Cov':>5} {'EM%':>7} {'Key Driver'}"
    print(hdr)
    print(f"  {'-' * 135}")

    for card in trade_cards:
        bias = _bias_label(card.direction_score)
        vb = _vol_bar(card.volatility_edge_score)
        em = f"{card.expected_move_pct:.1f}%" if card.expected_move_pct > 0 else ""
        driver = (card.key_thesis or "")[:55]
        wl = "*" if watchlist and card.ticker in watchlist else " "

        print(
            f"  {wl:>3} {card.ticker:<7} {card.cluster:<23} {bias:<10} "
            f"{card.direction_score:+6.1f} {card.volatility_edge_score:>4.0f} {vb} "
            f"{card.confidence_score:>4.0f}% {em:>7} {driver}"
        )

    print(f"  {'-' * 135}")
    clusters = {}
    biases = {}
    for c in trade_cards:
        clusters[c.cluster] = clusters.get(c.cluster, 0) + 1
        b = _bias_label(c.direction_score)
        biases[b] = biases.get(b, 0) + 1
    print(f"  {len(trade_cards)} events | {date.today().isoformat()} | ", end="")
    print("  ".join(f"{k}: {v}" for k, v in sorted(biases.items())))
    print(f"  Clusters: " + "  ".join(f"{k}: {v}" for k, v in sorted(clusters.items())))
    print()
