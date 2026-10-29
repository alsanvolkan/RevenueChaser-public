SPX · Price yfinance ^GSPC · Report date 2026-08-14 · Sessions 2026-08-03 → 2026-08-14 · Last sync: 14 Aug EOD · Unusual Whales API

## At a Glance — Single Source of Truth

| Metric | Value |
|---|---|
| Spot | 7,784 |
| Net GEX | +374 |
| Call Wall | 7,900 |
| Put Support | 7,500 |
| Gamma Flip (all-tenor) | 7,821 |
| Gamma Flip (0DTE) | 7,692 |
| Charm | 8,000 (-50) |
| Vanna | 7,850 (+15.0M) |
| Expected Move | 7,747 – 7,821 |
| Regime | Short Gamma — Amplifying |

## Gamma flip methods

| Method | Flip | Spot is | Note |
|---|---|---|---|
| UW flip, open interest (decides the regime) | 7,821 | spot below (short gamma) | UW gex-levels source=oi |
| UW flip, volume | 7,692 | spot above (long gamma) | UW gex-levels source=vol |
| Local cumulative crossing, open interest book | N/A | — | interpolated zero of cumulative OI gamma across strikes, nearest to spot |


UW does not document how it computes its gamma flip, and the open-interest and volume flips can differ. The regime follows the open-interest flip; the other rows are cross-checks and do not vote.

## DTE Band Walls

| DTE Band | Put Wall | Put Wall Net GEX | Call Wall | Call Wall Net GEX |
|---|---|---|---|---|
| 0-30 | 7,750 | -140 | 7,900 | -10 |
| 31-90 | 7,750 | +28 | 7,900 | +49 |
| 91-180 | 7,700 | +38 | 8,000 | +19 |
| 181-365 **(Put Wall standard)** | 7,500 | +58 | 7,900 | +9 |
| 365+ | N/A | N/A | N/A | N/A |
| ALL | 7,500 | +60 | 7,900 | +48 |


Put Wall (main level) is the 181-365 day band. The ALL row is the all-expiry reference.

## Summary

**Delta Bullish · Gamma Amplifying · Vanna Long · Charm Agree**


#### Delta — TREND `Bullish`

Net dealer Δ +$563.00M, bullish lean

| Metric | Value | Note |
|---|---|---|
| Net DEX | +$563.00M | at spot +$180.00M |
| Above spot | +$393.00M | upper strikes |
| Below spot | +$170.00M | lower strikes |


#### Gamma — MAGNET `Amplifying`

Short Γ → Amplifying regime. Largest level is the call wall at $7,900, dealers may sell into rallies as price approaches it.

| Metric | Value | Note |
|---|---|---|
| Net dealer Γ | +$374 | Short (spot below flip) |
| Γ vs prev close | +$32 (+9%) |  |
| Top wall | $7,900 | +$198 |
| Flip distance | +0.5% | at $7,820.63 |
| 0–1d rolls off | +$54.5K | by tomorrow |


#### Vanna — VOLATILITY `Long`

Long Vanna — IV spikes pressure stock lower via dealer selling.

| Metric | Value | Note |
|---|---|---|
| Net Vanna | +$15.00M | Long |
| Top vol-sensitive strike | $7,650 | +$30.00M |
| Δ from +1pt IV | +$45.00M |  |


#### Charm — TIMER `Agree`

Charm signals agree — sell-side lean.

| Metric | Value | Note |
|---|---|---|
| Live charm | -$80 | Sell pressure |
| Positioning | -$45.00M | Sell pressure · 100% imbalance |
| Where it matters | $8,000 | +2.8% from spot · pin zone |


## SPX Option Position Map

![SPX Option Position Map](chart.png)

## Liquidity Layer — Dealer Flow

| Band | Range | Net GEX | ~ /25pt |
|---|---|---|---|
| Absorption (positive gamma) | 7,800–8,000 | +623 | 77.9 |
| Air Gap (negative gamma) | 7,700–7,750 | -249 | 124.5 |

Zero crossing (approx. gamma flip): **7,800**

## Cross-Signal Synthesis

**Divergence** — 1/3 directional signals lean bullish, 2/3 lean bearish.

| Signal | Lean |
|---|---|
| Delta | Bullish |
| Gamma | Amplifying |
| Vanna | Long |
| Charm | Agree |

## Greek Relations — How They Interact

1. **IV rises** (↑IV): Implied volatility spikes (fear event or vol buying flow).
2. **Vanna moves delta** (Vanna): Vanna translates the IV shock into dealer delta. With net vanna SHORT, delta falls; with LONG vanna, delta rises.
3. **Dealer re-hedges** (BUY/SELL): Dealer buys stock to restore flat delta when delta fell (short vanna), or sells when delta rose (long vanna).
4. **Move damped / amplified** (Effect): Short vanna + positive gamma = the buying cushions the move (double buffer on the downside). Long vanna or negative gamma adds fuel.


### Gamma — Negative gamma — amplifying regime

Spot 7,784 sits below the gamma flip 7,821 (+0.5%). Dealers trade WITH the move: they sell into weakness and buy into strength, which accelerates moves.

| Metric | Value | Note |
|---|---|---|
| Net dealer Γ | +$374 | Short |

> TR: Fiyat 7,784, gamma flip'in (7,821, +0.5%) ALTINDA. Piyasa yapıcılar hareketle AYNI YÖNDE işlem yapar: düşerken satar, çıkarken alır; bu yüzden hareket hızlanır, kırılımlar uzar.

### Vanna — Long vanna — IV spikes pressure lower

Net vanna is +$45.00M. If IV jumps, dealer delta RISES, so dealers SELL stock to re-hedge — an IV spike adds fuel to the downside.

| Metric | Value | Note |
|---|---|---|
| Net Vanna | +$45.00M | Long |

> TR: Net vanna +$45.00M (pozitif). IV sıçrarsa dealer delta'sı YÜKSELİR ve dealer dengelemek için STOK SATAR — vol sıçraması aşağı baskıyı büyütür.

### Charm — Negative charm — mechanical selling into the close

Net charm is -$80. As expiry approaches, dealer delta builds; dealers mechanically SELL toward the close.

| Metric | Value | Note |
|---|---|---|
| Net Charm | -$80 | Sell pressure |
| Charm Peak | $8,000 | +2.8% from spot |

> TR: Net charm -$80 (negatif). Vade yaklaştıkça dealer delta'sı birikir; dealer kapanışa doğru mekanik SATIŞ yapar.

### Delta — Net delta bullish lean

Net dealer delta is +$563.00M — a bullish structural lean.

| Metric | Value | Note |
|---|---|---|
| Net DEX | +$563.00M | All strikes |

> TR: Net dealer delta +$563.00M — yukarı yönlü yapısal eğilim.

### Vomma — Vomma — vega's sensitivity to IV (2nd order)

Vomma measures how vega itself changes as IV moves. When vega is large and Vomma is positive, an IV spike makes the book's vol exposure grow, which can force accelerated re-hedging. It is a second-order effect on top of Vanna.

| Metric | Value | Note |
|---|---|---|
| Order | 2nd | above Vanna |

> TR: Vomma, IV değişince vega'nın kendisinin ne kadar değiştiğini ölçer (2. derece). Vega büyük ve Vomma pozitifken IV sıçraması vol pozisyonunu büyütür ve dengeleme hızlanır. Vanna'nın üstüne binen ikinci derece bir etkidir.

## Volatility Board

What it tells you: whether implied vol is cheap or expensive, how the term structure slopes, and what the skew costs.







## Options Volume & P/C

What it tells you: whether options flow is protective (put-heavy) or directional, and how rich the premium is.


Options volume data unavailable.


## Max Pain

What it tells you: the strike where the most options expire worthless; a structural gravity center, NOT a directional signal.

| Expiry | Max Pain | % from Spot |
|---|---|---|

## CFTC COT — E-mini S&P 500 (TFF)

What it tells you: how big players (asset managers, leveraged funds, dealers) are positioned; weekly-delayed but regular.


CFTC COT data unavailable.


## Scenario Set

| Scenario | Range | P | R:R | Expectancy E | Trade |
|---|---|---|---|---|---|
| Consolidation | 7,821 – 7,920 | 0.45 | n/a | n/a | Theta-weighted (directionless) |
| Upside | 7,920 – 8,000 | 0.25 | 0.8050719533058276 | -0.5487320116735431 | Directional long |
| Downside | 7,731 – 7,821 | 0.3 | 0.9057059474690561 | -0.42828821575928316 | Directional short |


Probabilities are heuristic, derived from the gamma regime and positioning; they are NOT calibrated forecasts. Bands derive from this week's levels only (gamma flip / call wall / put support), with the same intuitive offsets as the reference report. The Brier scorecard below tracks how honest these probabilities turn out to be.


## Iron Butterfly — Pin Harvest

**Signal: `BEARISH_BREAKOUT`** — Cancel pin; long put / bear debit spread into the short-gamma breakdown → target 7,650

Center pin: **7,800** · Wing width: **35** (EM 37 pts @ IV 0.08)

| Leg | Action | Type | Strike | Mid | IV | Θ /day |
|---|---|---|---|---|---|---|
| 1 | BUY | PUT | 7,765 | 6.00 | 10.0% | -2.0 |
| 2 | SELL | PUT | 7,800 | 15.00 | 10.0% | -7.0 |
| 3 | SELL | CALL | 7,800 | 20.00 | 10.0% | -8.0 |
| 4 | BUY | CALL | 7,835 | 5.00 | 10.0% | -3.0 |


Net credit **+24** · Max risk **11** · R:R **1 : 2.18** · Net Θ/day **10** · Breakevens **7,776 – 7,824**

Alternate widths: **25 pts** (credit 24, R:R 1 : 2.18) · **50 pts** (credit 22, R:R 1 : 0.82)



## Level Migration vs Prior Snapshot

| Level | This Week | Previous | Δ |
|---|---|---|---|
| Net GEX | 374 | N/A | N/A |
| Gamma Flip | 7,821 | N/A | N/A |
| Call Wall | 7,900 | N/A | N/A |
| Put Support | 7,500 | N/A | N/A |
| Gamma Magnet | 7,900 | N/A | N/A |

## Top-10 GEX Strikes

| # | Strike | Net GEX | Gamma-Flip Side |
|---|---|---|---|
| 1 | 7700 | -299.0 | below |
| 2 | 7800 | +250.0 | below |
| 3 | 7900 | +198.0 | above |
| 4 | 7850 | +130.0 | above |
| 5 | 7750 | +50.0 | below |
| 6 | 8000 | +45.0 | above |

## Expiry Distribution

| Expiry | DTE | Band | Net GEX | Net DEX |
|---|---|---|---|---|
| 2026-08-14 | 0.0 | 0DTE | +54.5K | +8.3M |
| 2026-08-21 | 7.0 | 1–7D | +10.0K | +50.0M |

## Brier Scorecard

Scored weeks:  · Avg Brier: — · Equal-weight ref: 

No scored weeks yet

## Details

| Level | Value | Reference Type | Method / Disclosure |
|---|---|---|---|
| Charm Peak | 8,000 (-50) | Strike & Signed Charm | Highest abs net Charm in Mode A (directionalized volume). Status: Charm Peak (distance 2.8%, dominance 1.67x) |
| Call Wall | 7,900 | Provider GEX Level | Largest positive net gamma above spot — a magnetic ceiling; dealers may sell into rallies as price approaches. Role: threshold (breakout risk). |
| Gamma Magnet | 7,900 | High-GEX Concentration | Provider gex-levels output: largest-magnitude net gamma concentration. |
| 0DTE Gamma Concentration | 7,700 | Intraday Concentration | Active expiration 2026-08-14: maximum absolute call_gex + put_gex. |
| SPX Close / Spot | 7,784 | Market Reference | Last close in the stored candle window (yfinance ^GSPC). |
| 0DTE Put Concentration | 7,700 | Intraday Put Concentration | Active expiration 2026-08-14: most negative put_gex. |
| Gamma Flip | 7,821 | Regime Threshold | The volatility turning point — above = positive gamma (calm), below = negative gamma (wild). |
| Put Wall | 7,500 | Provider GEX Level | Largest positive net gamma below spot — a trampoline floor; dealers may buy into dips as price approaches. Role: threshold (breakout risk). |
| 0DTE Cumulative GEX Crossing | 7,825 | Table-Only Proxy | Interpolated zero-crossing of cumulative same-day net GEX. Not a scenario flip. |
| Vanna Call Peak | 7,850 (+15.0M) | Directionalized Volume | Highest call_vanna_vol within ±25% of the as-of spot (window scales with spot, re-derived per report date). |
| Vanna Put Peak | 7,650 (+30.0M) | Directionalized Volume | Highest put_vanna_vol within ±25% of the as-of spot (window scales with spot, re-derived per report date). |
| One-Day Implied Range (1σ) | 7,747 – 7,821 | 1-Day Implied Move | Spot × IV / √252 = ±37.3 pts. Normal 1σ approximation from 1-day IV. |

## How to read this map (Gamma)

- Call Wall (7,900): magnetic ceiling — dealers may sell into rallies (take-profit zone).
- Put Wall (7,500): trampoline floor — dealers may buy into dips (bounce zone).

Gamma Flip (7,821) — the volatility turning point:

```
[ ABOVE FLIP LEVEL ]  ->  Positive Gamma Zone  ->  Volatility Suppressed (Calm)
-------------------------------------------------------------------------------
[ GAMMA FLIP LEVEL ]  ->  The Volatility Turning Point
-------------------------------------------------------------------------------
[ BELOW FLIP LEVEL ]  ->  Negative Gamma Zone  ->  Volatility Accelerated (Wild)
```

- Crossing below the flip -> Danger zone: reduce position sizes, expect wider price swings, or buy protective puts.
- Crossing above the flip -> Safety zone: expect steady, grinding upward movement with low volatility.

## Delta Map — Walkthrough

- Spot trades at 7,784. Around and just below spot, net delta concentration is relatively thin, so price can churn through this tight zone without major dealer-hedge interference.
- The delta Call Wall sits at 7,800 (net Δ +180,000,000), heavily call-dominated; dealers are net long delta here, so rallies toward it force mechanical selling — a heavy lid that caps upside unless buying volume overwhelms it.
- Deep liquidity pockets sit at 7,750 and 7,700; these institutional levels mix large put and call positions into sizeable net positive delta bars, so a drop toward them triggers dealer buying to rebalance — strong structural cushions below spot.

## Greek Sözlüğü & İlişkiler — Türkçe

Aşağıdaki tanımlar ve ilişkiler eğitim amaçlıdır; bu raporun İngilizce başlıklarıyla aynı veriden türetilmiştir.

- **Gamma (Γ)**: Spot fiyat 1 puan oynayınca delta'nın (dealer pozisyonunun) ne kadar değiştiği. Pozitif gamma: dealer hareketi söndürür. Negatif gamma: dealer hareketi büyütür.
- **Vanna**: IV (volatilite) değişince delta'nın ne kadar değiştiği. Negatif net vanna: IV yükselirse dealer alıcı olur → düşüş yastıklanır. Pozitif: IV yükselirse dealer satıcı olur → baskı artar.
- **Charm**: Vade yaklaştıkça delta'nın kendiliğinden erimesi. Pozitif charm: kapanışa doğru mekanik alış baskısı (pin etkisi). Negatif charm: mekanik satış baskısı.
- **Vomma**: IV değişince vega'nın ne kadar değiştiği (2. derece). IV sıçrarsa vol pozisyonunun büyüyüp dengelemeyi hızlandırıp hızlandırmayacağını gösterir.
- **HVL / Gamma Flip**: Kümülatif net GEX'in işaret değiştirdiği fiyat. Üstünde pozitif gamma (sakin), altında negatif gamma (hızlı).
- **Net GEX**: Tüm strike'lardaki net gamma maruziyeti. + milyon dolar = söndürücü kütle, − = hızlandırıcı.
- **Absorpsiyon bandı**: Kümülatif Net GEX'in pozitif olduğu bölge; dealer harekete karşı işlem yapar, fiyat orada zor ilerler.
- **Hava boşluğu (air gap)**: Kümülatif Net GEX'in negatif olduğu bölge; dealer hareketle aynı yönde işlem yapar, fiyat orada hızlı akar.
- **Max Pain**: En çok opsiyonun değersiz bittiği strike; yapısal 'çekim' seviyesi, yön sinyali değildir. Spot'tan uzaklığıyla okunur.
- **IV Rank**: Bugünkü IV'nin son 1 yıldaki konumu (%). Düşük = opsiyonlar ucuz, yüksek = pahalı.
- **Variance Risk Premium (VRP)**: Örtük oynaklığın (IV) gerçekleşen oynaklığın (RV) üzerindeki payı; kısa vol pozisyonunun ne kadar kazandırdığı. Yüksek = vol satıcısı zengin.
- **Put/Call oranı (P/C)**: Put hacminin call hacmine oranı. >1 put-ağır akış (koruma veya yönlü aşağı bahis); tek başına yön okunmaz, premium ve Net GEX ile birlikte yorumlanır.
- **Contango / Backwardation**: Vade yapısının yönü. Contango (kısa vade sakin, uzak vade pahalı) normaldir; backwardation (yakın vade daha pahalı) yakın stresin erken göstergesidir.
- **CFTC COT (TFF)**: Haftalık yayınlanan büyük oyuncu pozisyonları (asset manager, leveraged funds, dealer). Gecikmeli ama düzenli; yön ölçmek için kullanılır.

Bu haftaki veriye göre greek ilişkileri:

- **Gamma** — Fiyat 7,784, gamma flip'in (7,821, +0.5%) ALTINDA. Piyasa yapıcılar hareketle AYNI YÖNDE işlem yapar: düşerken satar, çıkarken alır; bu yüzden hareket hızlanır, kırılımlar uzar.
- **Vanna** — Net vanna +$45.00M (pozitif). IV sıçrarsa dealer delta'sı YÜKSELİR ve dealer dengelemek için STOK SATAR — vol sıçraması aşağı baskıyı büyütür.
- **Charm** — Net charm -$80 (negatif). Vade yaklaştıkça dealer delta'sı birikir; dealer kapanışa doğru mekanik SATIŞ yapar.
- **Delta** — Net dealer delta +$563.00M — yukarı yönlü yapısal eğilim.
- **Vomma** — Vomma, IV değişince vega'nın kendisinin ne kadar değiştiğini ölçer (2. derece). Vega büyük ve Vomma pozitifken IV sıçraması vol pozisyonunu büyütür ve dengeleme hızlanır. Vanna'nın üstüne binen ikinci derece bir etkidir.

Source: Unusual Whales API — open-position-based option exposure model. Report date: 2026-08-14. These levels are provider-model outputs and strike aggregations, not guaranteed support/resistance, pin certainty, forecasts, or price targets. Long vanna: an IV rise raises dealer delta and forces stock selling to re-hedge — a drag during vol spikes. A flow effect, not a support guarantee.