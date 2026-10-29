"""Greek Relations engine — how the greeks interact, sign-aware and data-driven.

This module answers the "if X then Y" questions about gamma/greek mechanics that
the Northstar report makes concrete, using the current snapshot's actual signs
(not hardcoded claims):

- Gamma  : spot vs gamma-flip position -> dampening vs amplifying regime.
- Vanna  : sign of net vanna -> what dealer does when IV rises.
- Charm  : sign of net charm -> mechanical buy/sell pressure toward expiry.
- Vomma  : second-order vega-vs-IV sensitivity (always framed as a caveat).

Each greek yields an EN headline (dashboard main language) plus a TR
explanation block rendered below the dashboard. The engine only reports what
the data says; unavailable values degrade to "unknown".
"""
from __future__ import annotations

from .charm_pin import MODE_DIRECTIONALIZED_VOLUME, charm_direction, net_charm, net_vanna


def _fmt_usd(v) -> str:
    if v is None:
        return "N/A"
    a = abs(v)
    sign = "+" if v >= 0 else "-"
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def _band_value(envelope: dict | None, kind: str) -> dict | None:
    if not envelope:
        return None
    band = envelope.get(kind)
    if not band:
        return None
    return {
        "range": f"{band['lo']:,.0f}–{band['hi']:,.0f}",
        "net_gex": _fmt_usd(band["net_gex"]),
        "per_25pt": f"{band['per_25pt']:,.1f}" if band.get("per_25pt") is not None else "N/A",
    }


def greek_relations(snapshot: dict) -> list[dict]:
    """Return per-greek relation cards (EN headline + TR explanation)."""
    spot = snapshot.get("current_level")
    detail = snapshot.get("regime_detail") or {}
    flip = (snapshot.get("levels") or {}).get("regime_threshold")
    # The regime wording quotes spot and flip, so it is only said when both exist (G2).
    state = detail.get("state") if (spot is not None and flip is not None) else "unknown"
    flip_pct = ((flip - spot) / spot * 100) if (flip is not None and spot) else None

    spot_expo = snapshot.get("spot_exposures", [])
    net_v = sum((net_vanna(r, MODE_DIRECTIONALIZED_VOLUME) or 0.0) for r in spot_expo) if spot_expo else None
    # The same sum as the Charm card's Live charm: rows without a charm value are no data, not a zero (review N1).
    charm_vals = [v for v in (net_charm(r, MODE_DIRECTIONALIZED_VOLUME) for r in spot_expo) if v is not None]
    net_c = sum(charm_vals) if charm_vals else None

    strike_rows = snapshot.get("strike_exposures", [])
    net_delta = sum((r.get("net_delta") or 0.0) for r in strike_rows) if strike_rows else None

    envelope = snapshot.get("gex_envelope")

    cards = []

    # ── Gamma ──
    if state == "long":
        gamma_head = "Positive gamma — dampening regime"
        gamma_effect_en = (
            f"Spot {spot:,.0f} sits above the gamma flip {flip:,.0f} "
            f"({flip_pct:+.1f}%). Dealers trade AGAINST the move: they sell rallies "
            "and buy dips, which damps volatility."
        )
        gamma_effect_tr = (
            f"Fiyat {spot:,.0f}, gamma flip'in ({flip:,.0f}, {flip_pct:+.1f}%) ÜZERİNDE. "
            "Piyasa yapıcılar hareketin TERSİNE işlem yapar: rallide satar, düşüşte alır; "
            "bu yüzden hareket söndürülür, gün içi salınım daralır."
        )
    elif state == "short":
        gamma_head = "Negative gamma — amplifying regime"
        gamma_effect_en = (
            f"Spot {spot:,.0f} sits below the gamma flip {flip:,.0f} "
            f"({flip_pct:+.1f}%). Dealers trade WITH the move: they sell into weakness "
            "and buy into strength, which accelerates moves."
        )
        gamma_effect_tr = (
            f"Fiyat {spot:,.0f}, gamma flip'in ({flip:,.0f}, {flip_pct:+.1f}%) ALTINDA. "
            "Piyasa yapıcılar hareketle AYNI YÖNDE işlem yapar: düşerken satar, çıkarken alır; "
            "bu yüzden hareket hızlanır, kırılımlar uzar."
        )
    elif state == "at_flip":
        gamma_head = "Gamma — spot is at the flip"
        gamma_effect_en = (f"Spot {spot:,.0f} sits on the gamma flip {flip:,.0f}: dealer hedging "
                           "changes sign here, so neither damping nor amplifying dominates.")
        gamma_effect_tr = (f"Fiyat {spot:,.0f}, gamma flip'in ({flip:,.0f}) tam üzerinde: "
                           "piyasa yapıcı hedge'i burada yön değiştirir, ne söndürme ne hızlandırma baskın.")
    else:
        gamma_head = "Gamma — regime unknown"
        gamma_effect_en = "Insufficient data to determine the gamma regime."
        gamma_effect_tr = "Gamma rejimini belirlemek için yeterli veri yok."
    cards.append({
        "greek": "Gamma",
        "symbol": "Γ",
        "headline": gamma_head,
        "english": gamma_effect_en,
        "turkish": gamma_effect_tr,
        "row": [("Net dealer Γ", _fmt_usd(snapshot.get("net_gamma_total")),
                 {"long": "Long", "short": "Short"}.get(state, "N/A"))],
    })

    # ── Vanna ──
    if net_v is None:
        vanna_head = "Vanna — unknown"
        vanna_en = "No net vanna data available."
        vanna_tr = "Net vanna verisi yok."
        vanna_row = ("Net Vanna", "N/A", "N/A")
    elif net_v < 0:
        vanna_head = "Short vanna — IV spikes cushion downside"
        vanna_en = (
            f"Net vanna is {_fmt_usd(net_v)}. If IV jumps, dealer delta FALLS, so "
            "dealers BUY stock to re-hedge — an IV spike puts a cushion under the move."
        )
        vanna_tr = (
            f"Net vanna {_fmt_usd(net_v)} (negatif). IV sıçrarsa dealer delta'sı DÜŞER ve "
            "dealer dengelemek için STOK ALIR — yani vol sıçraması düşüşü YASTIKLAR."
        )
        vanna_row = ("Net Vanna", _fmt_usd(net_v), "Short")
    else:
        vanna_head = "Long vanna — IV spikes pressure lower"
        vanna_en = (
            f"Net vanna is {_fmt_usd(net_v)}. If IV jumps, dealer delta RISES, so "
            "dealers SELL stock to re-hedge — an IV spike adds fuel to the downside."
        )
        vanna_tr = (
            f"Net vanna {_fmt_usd(net_v)} (pozitif). IV sıçrarsa dealer delta'sı YÜKSELİR ve "
            "dealer dengelemek için STOK SATAR — vol sıçraması aşağı baskıyı büyütür."
        )
        vanna_row = ("Net Vanna", _fmt_usd(net_v), "Long")
    cards.append({
        "greek": "Vanna",
        "symbol": "Vanna",
        "headline": vanna_head,
        "english": vanna_en,
        "turkish": vanna_tr,
        "row": [vanna_row],
    })

    # ── Charm ──
    if net_c is None:
        charm_head = "Charm — unknown"
        charm_en = "No net charm data available."
        charm_tr = "Net charm verisi yok."
        charm_row = ("Net Charm", "N/A", "N/A")
    elif net_c > 0:
        charm_head = "Positive charm — mechanical buying into the close"
        charm_en = (
            f"Net charm is {_fmt_usd(net_c)}. As expiry approaches, dealer delta decays; "
            "dealers mechanically BUY back toward the close (pin-side pressure)."
        )
        charm_tr = (
            f"Net charm {_fmt_usd(net_c)} (pozitif). Vade yaklaştıkça dealer delta'sı erir; "
            "dealer kapanışa doğru mekanik ALIŞ yapar (pin yönlü baskı)."
        )
        charm_row = ("Net Charm", _fmt_usd(net_c), charm_direction(net_c))
    elif net_c == 0:
        charm_head = "Flat charm — no mechanical pressure into the close"
        charm_en = "Net charm sums to zero: time decay leaves dealer delta unchanged, so it forces no hedge either way."
        charm_tr = "Net charm sıfır: zaman erimesi dealer delta'sını değiştirmiyor, mekanik alış ya da satış yok."
        charm_row = ("Net Charm", _fmt_usd(net_c), charm_direction(net_c))
    else:
        charm_head = "Negative charm — mechanical selling into the close"
        charm_en = (
            f"Net charm is {_fmt_usd(net_c)}. As expiry approaches, dealer delta builds; "
            "dealers mechanically SELL toward the close."
        )
        charm_tr = (
            f"Net charm {_fmt_usd(net_c)} (negatif). Vade yaklaştıkça dealer delta'sı birikir; "
            "dealer kapanışa doğru mekanik SATIŞ yapar."
        )
        charm_row = ("Net Charm", _fmt_usd(net_c), charm_direction(net_c))
    cq = snapshot.get("charm_qualification") or {}
    cards.append({
        "greek": "Charm",
        "symbol": "Charm",
        "headline": charm_head,
        "english": charm_en,
        "turkish": charm_tr,
        "row": [
            charm_row,
            (cq.get("label", "Charm Peak"), f"${cq.get('strike'):,.0f}" if cq.get("strike") else "N/A",
             f"{cq.get('distance_pct_from_spot', 0) * 100:+.1f}% from spot" if cq.get("distance_pct_from_spot") is not None else ""),
        ],
    })

    # ── Delta / DEX ──
    if net_delta is None:
        dex_head = "Net Delta — unknown"
        dex_en = "No net delta data available."
        dex_tr = "Net delta verisi yok."
    else:
        dex_head = "Net delta " + ("bullish lean" if net_delta > 0 else "bearish lean")
        dex_en = f"Net dealer delta is {_fmt_usd(net_delta)} — a {'bullish' if net_delta > 0 else 'bearish'} structural lean."
        dex_tr = f"Net dealer delta {_fmt_usd(net_delta)} — {'yukarı' if net_delta > 0 else 'aşağı'} yönlü yapısal eğilim."
    cards.append({
        "greek": "Delta",
        "symbol": "Δ",
        "headline": dex_head,
        "english": dex_en,
        "turkish": dex_tr,
        "row": [("Net DEX", _fmt_usd(net_delta), "All strikes")],
    })

    # ── Vomma (2nd order) ──
    cards.append({
        "greek": "Vomma",
        "symbol": "Vomma",
        "headline": "Vomma — vega's sensitivity to IV (2nd order)",
        "english": (
            "Vomma measures how vega itself changes as IV moves. When vega is large and "
            "Vomma is positive, an IV spike makes the book's vol exposure grow, which can "
            "force accelerated re-hedging. It is a second-order effect on top of Vanna."
        ),
        "turkish": (
            "Vomma, IV değişince vega'nın kendisinin ne kadar değiştiğini ölçer (2. derece). "
            "Vega büyük ve Vomma pozitifken IV sıçraması vol pozisyonunu büyütür ve dengeleme "
            "hızlanır. Vanna'nın üstüne binen ikinci derece bir etkidir."
        ),
        "row": [("Order", "2nd", "above Vanna")],
    })

    return cards


def causal_chain(snapshot: dict) -> list[dict]:
    """The IV -> Vanna -> Delta -> hedge chain with the current signs applied."""
    cards = {c["greek"]: c for c in greek_relations(snapshot)}
    vanna = next((c for c in cards.values() if c["greek"] == "Vanna"), None)
    short_vanna = vanna is not None and "Short vanna" in vanna["headline"]

    steps = [
        {
            "step": 1,
            "label": "IV rises",
            "icon": "↑IV",
            "english": "Implied volatility spikes (fear event or vol buying flow).",
            "turkish": "Örtük oynaklık (IV) sıçrar — korku artar veya vol alım akışı gelir.",
        },
        {
            "step": 2,
            "label": "Vanna moves delta",
            "icon": "Vanna",
            "english": ("Vanna translates the IV shock into dealer delta. With net vanna "
                        "SHORT, delta falls; with LONG vanna, delta rises."),
            "turkish": ("Vanna, IV şokunu dealer delta'sına çevirir. Net vanna NEGATİFSE "
                        "delta düşer; POZİTİFSE delta yükselir."),
        },
        {
            "step": 3,
            "label": "Dealer re-hedges",
            "icon": "BUY/SELL",
            "english": ("Dealer buys stock to restore flat delta when delta fell (short vanna), "
                        "or sells when delta rose (long vanna)."),
            "turkish": ("Dealer delta'sı düştüyse (short vanna) dengelemek için ALIR; "
                        "delta yükseldiyse (long vanna) SATAR."),
        },
        {
            "step": 4,
            "label": "Move damped / amplified",
            "icon": "Effect",
            "english": ("Short vanna + positive gamma = the buying cushions the move (double "
                        "buffer on the downside). Long vanna or negative gamma adds fuel."),
            "turkish": ("Short vanna + pozitif gamma = alış hareketi yastıklar (düşüşte çift "
                        "tampon). Long vanna ya da negatif gamma hareketi büyütür."),
        },
    ]

    if short_vanna:
        steps[2]["english"] = ("Dealer buys stock to restore flat delta — the cushion side. "
                               "Positive gamma reinforces this: both books damp the fall.")
        steps[2]["turkish"] = ("Dealer delta'sı düştüğü için dengelemek adına STOK ALIR — "
                               "yastık tarafı. Pozitif gamma da aynı yönde çalışır: iki defter de "
                               "düşüşü söndürür.")
        steps[3]["english"] = ("Downside cushioned: an IV spike finds a buyer on the other side "
                               "(short vanna) and a dampening dealer book (positive gamma).")
        steps[3]["turkish"] = ("Düşüş yastıklanır: IV sıçraması karşısında alıcı bulur "
                               "(short vanna) ve dealer kitabı söndürücüdür (pozitif gamma).")
    return steps


# Static educational glossary rendered as the TR block under the dashboard.
GLOSSARY_TR = [
    {"greek": "Gamma (Γ)", "def": "Spot fiyat 1 puan oynayınca delta'nın (dealer pozisyonunun) ne kadar değiştiği. "
        "Pozitif gamma: dealer hareketi söndürür. Negatif gamma: dealer hareketi büyütür."},
    {"greek": "Vanna", "def": "IV (volatilite) değişince delta'nın ne kadar değiştiği. Negatif net vanna: "
        "IV yükselirse dealer alıcı olur → düşüş yastıklanır. Pozitif: IV yükselirse dealer satıcı olur → baskı artar."},
    {"greek": "Charm", "def": "Vade yaklaştıkça delta'nın kendiliğinden erimesi. Pozitif charm: kapanışa doğru "
        "mekanik alış baskısı (pin etkisi). Negatif charm: mekanik satış baskısı."},
    {"greek": "Vomma", "def": "IV değişince vega'nın ne kadar değiştiği (2. derece). IV sıçrarsa vol pozisyonunun "
        "büyüyüp dengelemeyi hızlandırıp hızlandırmayacağını gösterir."},
    {"greek": "HVL / Gamma Flip", "def": "Kümülatif net GEX'in işaret değiştirdiği fiyat. Üstünde pozitif gamma "
        "(sakin), altında negatif gamma (hızlı)."},
    {"greek": "Net GEX", "def": "Tüm strike'lardaki net gamma maruziyeti. + milyon dolar = söndürücü kütle, "
        "− = hızlandırıcı."},
    {"greek": "Absorpsiyon bandı", "def": "Kümülatif Net GEX'in pozitif olduğu bölge; dealer harekete karşı "
        "işlem yapar, fiyat orada zor ilerler."},
    {"greek": "Hava boşluğu (air gap)", "def": "Kümülatif Net GEX'in negatif olduğu bölge; dealer hareketle aynı "
        "yönde işlem yapar, fiyat orada hızlı akar."},
    {"greek": "Max Pain", "def": "En çok opsiyonun değersiz bittiği strike; yapısal 'çekim' seviyesi, yön sinyali "
        "değildir. Spot'tan uzaklığıyla okunur."},
    {"greek": "IV Rank", "def": "Bugünkü IV'nin son 1 yıldaki konumu (%). Düşük = opsiyonlar ucuz, yüksek = pahalı."},
    {"greek": "Variance Risk Premium (VRP)", "def": "Örtük oynaklığın (IV) gerçekleşen oynaklığın (RV) üzerindeki "
        "payı; kısa vol pozisyonunun ne kadar kazandırdığı. Yüksek = vol satıcısı zengin."},
    {"greek": "Put/Call oranı (P/C)", "def": "Put hacminin call hacmine oranı. >1 put-ağır akış (koruma veya "
        "yönlü aşağı bahis); tek başına yön okunmaz, premium ve Net GEX ile birlikte yorumlanır."},
    {"greek": "Contango / Backwardation", "def": "Vade yapısının yönü. Contango (kısa vade sakin, uzak vade pahalı) "
        "normaldir; backwardation (yakın vade daha pahalı) yakın stresin erken göstergesidir."},
    {"greek": "CFTC COT (TFF)", "def": "Haftalık yayınlanan büyük oyuncu pozisyonları (asset manager, leveraged "
        "funds, dealer). Gecikmeli ama düzenli; yön ölçmek için kullanılır."},
]
