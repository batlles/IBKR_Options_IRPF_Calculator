"""
IBKR Stock P&L Calculator para IRPF  [v2026-05-31]
=====================================================
Lógica:
  1. Lee todos los TradeConfirm del XML exportado desde IBKR (FlexQuery o Activity Statement).
  2. Para cada subyacente, localiza los movimientos STK y las opciones que los causaron.
  3. Ajusta el valor de adquisición/transmisión con las primas netas de las opciones.
  4. Aplica FIFO (por defecto IBKR; con -f, FIFO estricto según art. 37.2 LIRPF).
  5. Imprime tabla resumen: subyacente | adquisición | transmisión | P&L + CSV para Excel.

Uso básico:
    python ibkr_pl_options.py <trades.xml> [opciones]

Opciones:
    -f / --fifo               FIFO estricto (requerido por Hacienda España, art. 37.2 LIRPF).
                              ⚠️  Usa SIEMPRE -f para la declaración de la renta.
                              Sin este flag el script intenta reproducir el resultado de IBKR,
                              que puede diferir del FIFO legal en casos de ejercicios simultáneos.

    -v / --verbose            Muestra el detalle de cada tramo (primas, comisiones, lotes).

    --basis SYM:QTY:COSTE     Informa un lote de compra previo al período del XML.
                              Necesario cuando las acciones se compraron antes de la fecha
                              de inicio del XML y el script muestra ⚠️ sin lote de compra.
                              SYM   = símbolo del subyacente (ej. CLSK, KO)
                              QTY   = número de acciones del lote
                              COSTE = coste total del lote en USD (usar punto decimal),
                                      tomado directamente de la columna "Basis" del HTML
                                      de IBKR (valor absoluto).
                              Se puede repetir para informar varios lotes del mismo símbolo
                              (en orden cronológico, el más antiguo primero).

    --lifo-sell SYM:YYYYMMDD  Fuerza LIFO en una venta concreta.
                              Úsalo cuando el XML exportado tiene code="C;Ex" pero el HTML
                              de IBKR muestra "C;Ex;LI" (IBKR a veces omite el código LI
                              en el XML). Fecha = tradeDate del trade en el XML.
                              Se puede repetir para varias ventas.

Ejemplos:
    # Caso normal: FIFO IRPF con toda la información en el XML
    python ibkr_pl_options.py mi_export_2025.xml -f

    # Con detalle de cada tramo
    python ibkr_pl_options.py mi_export_2025.xml -f -v

    # CLSK / KO: acciones compradas antes del período del XML
    # (el script avisará con ⚠️ y te dirá qué símbolo necesita --basis)
    # El COSTE lo encuentras en la columna "Basis" del HTML resumen de IBKR
    python ibkr_pl_options.py mi_export_2025.xml -f \
        --basis CLSK:100:1689.56 \
        --basis CLSK:200:3103.22 \
        --basis KO:100:6575.81

    # AMZN: el XML exportado omite el código LI en una venta C;Ex;LI
    # (sin --lifo-sell el resultado IBKR difiere; para IRPF usa -f igualmente)
    python ibkr_pl_options.py mi_export_2025.xml --lifo-sell AMZN:20251121

    # Combinando varias opciones
    python ibkr_pl_options.py mi_export_2025.xml -f \
        --basis CLSK:100:1689.56 --basis CLSK:200:3103.22 \
        --basis KO:100:6575.81 \
        --lifo-sell AMZN:20251121

Notas sobre limitaciones conocidas:
    - NVDA (y otros casos con ejercicios simultáneos en el mismo día): IBKR usa un
      criterio de emparejamiento económico de lotes (no FIFO puro) que no es posible
      generalizar sin romper otros subyacentes. Sin -f el script da FIFO estricto;
      el resultado puede diferir del HTML de IBKR en ±200-300 USD. Para Hacienda
      el resultado con -f es el correcto.
    - Posiciones split-ajustadas (ej. NFLX tras split 10:1): soportadas mediante
      pro-rateo automático de la prima cuando se detecta que un opener cubre
      más contratos de los esperados.
"""

import xml.etree.ElementTree as ET
import sys
from collections import defaultdict
from dataclasses import dataclass, field


# ───────────────────────────────────────────────
# Parsing
# ───────────────────────────────────────────────

@dataclass
class T:
    desc: str
    date: str
    dt: str
    conid: str
    codes: set
    underlying: str
    strike: float
    expiry: str
    put_call: str
    buy_sell: str
    qty: float
    price: float
    commission: float
    proceeds: float
    net_cash: float
    asset: str   # STK | OPT


def copy_t(t: 'T', **overrides) -> 'T':
    """Copia un trade T con campos sobreescritos (para ajuste de prima split)."""
    import dataclasses
    return dataclasses.replace(t, **overrides)

def parse(path: str) -> list[T]:
    root = ET.parse(path).getroot()
    out = []
    for e in root.iter('TradeConfirm'):
        def g(a, d=''):  return e.get(a, d)
        def gf(a):
            try: return float(g(a, '0'))
            except: return 0.0
        out.append(T(
            desc=g('description'), date=g('tradeDate'), dt=g('dateTime'),
            conid=g('conid'), codes=set(g('code').split(';')),
            underlying=g('underlyingSymbol'), strike=gf('strike'),
            expiry=g('expiry'), put_call=g('putCall'),
            buy_sell=g('buySell'), qty=gf('quantity'),
            price=gf('price'), commission=gf('commission'),
            proceeds=gf('proceeds'), net_cash=gf('netCash'),
            asset=g('assetCategory'),
        ))
    out.sort(key=lambda x: x.dt)
    return out


# ───────────────────────────────────────────────
# Lógica principal
# ───────────────────────────────────────────────

@dataclass
class Leg:
    """Un tramo de una posición STK: apertura o cierre."""
    date: str
    qty: float                  # positivo = compra, negativo = venta
    gross: float                # proceeds brutos (positivo = ingreso)
    stock_commission: float     # comisión del trade STK
    option_premium_net: float   # prima(s) neta(s) de la opción que la causó
    codes: set = None           # codes del STK trade (LI = LIFO, etc.)
    note: str = ''


def find_option_openers(conid: str, trades: list[T]) -> list[T]:
    """Dado un conid de opción, devuelve TODOS los trades de apertura (code O).
    Puede haber varios si se abrió en múltiples tickets (fills parciales)."""
    return [t for t in trades if t.conid == conid and t.asset == 'OPT' and 'O' in t.codes]



def total_closing_qty_for_conid(conid: str, trades: list) -> float:
    """Suma de abs(qty) de todos los cierres de opción para este conid.
    Se usa para detectar contratos split-ajustados (un opener antiguo cubre
    múltiples contratos nuevos post-split) y pro-ratear su prima."""
    closing_codes = {'A', 'C', 'Ex', 'Ep'}
    total = 0.0
    for t in trades:
        if t.conid == conid and t.asset == 'OPT':
            if closing_codes & t.codes and 'O' not in t.codes:
                total += abs(t.qty)
    return total

def find_options_for_assignment(stk: T, trades: list[T], used_opener_ids: set) -> list[T]:
    """
    Busca las opciones que causaron un movimiento STK de asignación/ejercicio.
    used_opener_ids: evita que el mismo opener se cuente en dos STK trades distintos.

    Codes especiales que cambian el tratamiento:
    - IA;O : Internal Assignment Open → compra real pero sin prima propia. Sin ajuste.
    - Ex;O : artefacto contable del ejercicio simultáneo al C;Ex. Se ignora.
    - C;P  : cierre parcial en mercado → venta normal sin opción que la cause.
    """
    is_buy  = stk.qty > 0
    stk_codes = stk.codes

    # IA;O = asignación interna: compra real pero sin prima propia → sin ajuste
    if 'IA' in stk_codes:
        return []

    # Ventas de mercado puro (C, C;P, O…) sin A ni Ex → sin opción que la cause
    if not is_buy and 'A' not in stk_codes and 'Ex' not in stk_codes:
        return []

    # Contratos que corresponden a este trade (1 contrato = 100 acciones)
    contracts_needed      = int(round(abs(stk.qty) / 100))
    contracts_allocated   = 0   # contratos cuya prima ya está asignada (para splits)

    assigners = []
    for t in trades:
        if not (t.asset == 'OPT'
                and t.underlying == stk.underlying
                and t.date == stk.date):
            continue

        openers_list = find_option_openers(t.conid, trades)
        if not openers_list:
            continue
        opener = openers_list[0]  # representativo para put_call / buy_sell

        if is_buy:
            # COMPRA por asignación de Put vendida (code A en el cierre OPT).
            # Soporte para contratos split-ajustados: un opener antiguo puede cubrir
            # más contratos nuevos que abs(t.qty). Pro-rateamos la prima por
            # (closer_qty / total_closers_para_este_conid).
            if 'A' in t.codes and opener.put_call == 'P' and opener.buy_sell == 'SELL':
                if id(t) not in used_opener_ids:
                    closer_contracts = int(round(abs(t.qty)))
                    total_exit_qty   = total_closing_qty_for_conid(t.conid, trades)
                    is_split         = len(openers_list) < total_exit_qty  # split: un opener cubre múltiples cierres
                    taken = 0
                    for o in openers_list:
                        if id(o) not in used_opener_ids and taken < closer_contracts and contracts_allocated < contracts_needed:
                            # Pro-ratear si hay split (un opener antiguo cubre múltiples contratos nuevos)
                            scale = closer_contracts / total_exit_qty if is_split else 1.0
                            scaled_o = copy_t(o, net_cash=o.net_cash * scale)
                            assigners.append(scaled_o)
                            # En caso split NO marcamos el opener como usado: puede servir a otros closers
                            if not is_split:
                                used_opener_ids.add(id(o))
                            taken += 1
                    if taken > 0:
                        used_opener_ids.add(id(t))
                        contracts_allocated += closer_contracts  # contratos cubiertos
        else:
            # VENTA por Call vendida asignada (code A en el cierre OPT).
            if 'A' in t.codes and opener.put_call == 'C' and opener.buy_sell == 'SELL':
                if id(t) not in used_opener_ids:
                    closer_contracts = int(round(abs(t.qty)))
                    taken = 0
                    for o in openers_list:
                        if id(o) not in used_opener_ids and taken < closer_contracts and contracts_allocated < contracts_needed:
                            assigners.append(o)
                            used_opener_ids.add(id(o))
                            taken += 1
                    if taken > 0:
                        used_opener_ids.add(id(t))
                        contracts_allocated += closer_contracts
            # VENTA por Put comprada ejercida (code Ex en el cierre OPT).
            # Pro-rateamos la prima si el opener cubre más contratos que este closer
            # (caso de contratos split-ajustados post reverse/forward split).
            elif 'Ex' in t.codes and opener.put_call == 'P' and opener.buy_sell == 'BUY':
                if id(t) not in used_opener_ids:
                    closer_contracts = int(round(abs(t.qty)))
                    total_exit_qty   = total_closing_qty_for_conid(t.conid, trades)
                    is_split         = len(openers_list) < total_exit_qty  # split: un opener cubre múltiples cierres
                    taken = 0
                    for o in openers_list:
                        if id(o) not in used_opener_ids and taken < closer_contracts and contracts_allocated < contracts_needed:
                            scale = closer_contracts / total_exit_qty if is_split else 1.0
                            scaled_o = copy_t(o, net_cash=o.net_cash * scale)
                            assigners.append(scaled_o)
                            if not is_split:
                                used_opener_ids.add(id(o))
                            taken += 1
                    if taken > 0:
                        used_opener_ids.add(id(t))
                        contracts_allocated += closer_contracts

        # Parar cuando ya hemos cubierto todos los contratos de este STK trade
        if contracts_allocated >= contracts_needed:
            break

    return assigners



def process(trades: list[T], force_fifo: bool = False, initial_lots: dict = None, lifo_sells: set = None):
    # Agrupamos los STK por subyacente
    stk_by_sym = defaultdict(list)
    for t in trades:
        if t.asset == 'STK':
            stk_by_sym[t.underlying].append(t)

    results = []

    for sym, stk_trades in sorted(stk_by_sym.items()):
        # Para cada STK buscamos sus opciones relacionadas.
        # used_opener_ids garantiza que cada opener OPT se imputa solo a UN trade STK.
        legs = []
        used_opener_ids: set = set()
        for stk in stk_trades:
            # Ex;O SELL = venta real que cierra lotes, busca su put ejercida igual que C;Ex.
            openers = find_options_for_assignment(stk, trades, used_opener_ids)

            # Prima neta total = suma de net_cash de los openers
            # (net_cash ya lleva comisión descontada)
            premium_net = sum(o.net_cash for o in openers)

            note_parts = [f"{o.desc} prima_neta={o.net_cash:.4f}" for o in openers]

            legs.append(Leg(
                date=stk.date,
                qty=stk.qty,
                gross=-stk.proceeds if stk.qty > 0 else stk.proceeds,
                # Para compra: gross es el desembolso (positivo)
                # Para venta: gross es el ingreso (positivo)
                stock_commission=abs(stk.commission),
                option_premium_net=premium_net,
                codes=stk.codes,
                note='; '.join(note_parts),
            ))

        # FIFO / LIFO: casamos cada venta con la compra correspondiente.
        # IBKR usa LIFO cuando el code STK contiene 'LI' (Last-In First-Out).
        import copy
        # Lotes iniciales (compras previas al período del XML) al frente de la cola
        pre_lots = []
        if initial_lots and sym in initial_lots:
            for (qty, total_cost) in initial_lots[sym]:
                pre_lots.append(Leg(
                    date='00000000',   # más antiguo → siempre primero en FIFO
                    qty=qty,
                    gross=total_cost,
                    stock_commission=0.0,
                    option_premium_net=0.0,
                    codes=set(),
                    note=f'lote previo {qty}acc coste={total_cost}',
                ))
        buy_queue = pre_lots + [copy.copy(l) for l in legs if l.qty > 0]
        sell_list = [l for l in legs if l.qty < 0]

        closed_acq = 0.0
        closed_tra = 0.0

        for sell in sell_list:
            qty_to_close = abs(sell.qty)
            sell_unit = (sell.gross - sell.stock_commission + sell.option_premium_net) / qty_to_close
            # force_fifo=True (modo IRPF España) ignora el code LI de IBKR
            # lifo_sells: conjunto de (sym, date) forzados a LIFO (cuando el XML omite el código LI)
            _is_forced_lifo = lifo_sells is not None and (sym, sell.date) in lifo_sells
            use_lifo = (not force_fifo) and (
                (sell.codes is not None and 'LI' in sell.codes) or _is_forced_lifo
            )

            while qty_to_close > 0 and buy_queue:
                # LIFO: buscar el lote de asignación (A;O) más reciente (Last-In).
                # IBKR empareja C;Ex;LI contra el último lote asignado, no el último
                # comprado en mercado (O;P), por eso buscamos el más reciente con 'A'.
                if use_lifo:
                    lifo_idx = next(
                        (i for i in range(len(buy_queue)-1, -1, -1)
                         if buy_queue[i].codes and 'A' in buy_queue[i].codes),
                        len(buy_queue) - 1   # fallback al último si no hay A
                    )
                    buy = buy_queue[lifo_idx]
                else:
                    lifo_idx = 0
                    buy = buy_queue[0]
                take = min(qty_to_close, buy.qty)
                frac = take / buy.qty
                buy_unit = (buy.gross + buy.stock_commission - buy.option_premium_net) / buy.qty

                closed_acq += buy_unit * take
                closed_tra += sell_unit * take

                buy.qty -= take
                buy.gross              *= (1 - frac)   # reducir el gross restante
                buy.option_premium_net *= (1 - frac)
                buy.stock_commission   *= (1 - frac)
                qty_to_close -= take

                if buy.qty <= 0:
                    buy_queue.pop(lifo_idx)  # quitar el lote consumido (FIFO=0, LIFO=idx)


        open_legs = [l for l in buy_queue if l.qty > 0]


        pl = closed_tra - closed_acq

        # Advertir si hay ventas sin lotes de compra disponibles
        unmatched_sells = sum(abs(l.qty) for l in sell_list) - (closed_tra / (sell_list[0].gross / abs(sell_list[0].qty)) if sell_list and sell_list[0].gross else 0)
        _total_sell_qty  = sum(abs(l.qty) for l in sell_list)
        _total_buy_avail = sum(l.qty for l in [copy.copy(x) for x in legs if x.qty > 0])
        if initial_lots and sym in initial_lots:
            _total_buy_avail += sum(q for q, _ in initial_lots[sym])
        if _total_sell_qty > _total_buy_avail + 0.01:
            _missing = _total_sell_qty - _total_buy_avail
            print(f"  ⚠️  {sym}: {_missing:.0f} acciones vendidas sin lote de compra en el XML.")
            print(f"      Usa --basis {sym}:QTY:COSTE_TOTAL para informar el coste de adquisición previo.")

        results.append({
            'symbol':       sym,
            'acquisition':  closed_acq,
            'transmission': closed_tra,
            'pl':           pl,
            'legs':         legs,
            'open_legs':    open_legs,
        })

    return results


# ───────────────────────────────────────────────
# Output
# ───────────────────────────────────────────────

def print_results(results: list[dict], verbose: bool = False):
    W = 80
    col = [12, 18, 18, 14]  # símbolo, adq, trans, P&L

    header = (f"{'Símbolo':<{col[0]}}"
              f"{'Adquisición (USD)':>{col[1]}}"
              f"{'Transmisión (USD)':>{col[2]}}"
              f"{'P&L (USD)':>{col[3]}}")

    print()
    print("═" * W)
    print("  RESUMEN P&L PARA IRPF  (valores ajustados por primas de opciones)")
    print("═" * W)
    print(f"  {header}")
    print("  " + "─" * (W - 2))

    total_acq = total_tra = total_pl = 0.0

    for r in results:
        sym  = r['symbol']
        acq  = r['acquisition']
        tra  = r['transmission']
        pl   = r['pl']
        sign = '✅' if pl >= 0 else '❌'

        total_acq += acq
        total_tra += tra
        total_pl  += pl

        print(f"  {sym:<{col[0]}}"
              f"{acq:>{col[1]},.2f}"
              f"{tra:>{col[2]},.2f}"
              f"{pl:>{col[3]},.2f}  {sign}")

        if verbose:
            for l in r['legs']:
                tag = 'COMPRA' if l.qty > 0 else 'VENTA '
                print(f"      {tag} {abs(l.qty):.0f} acc  {l.date}"
                      f"  bruto={l.gross:.2f}"
                      f"  prima_opc={l.option_premium_net:.4f}"
                      f"  com_stk={l.stock_commission:.4f}")
                if l.note:
                    for n in l.note.split(';'):
                        print(f"         → {n.strip()}")

    print("  " + "─" * (W - 2))
    sign_t = '✅' if total_pl >= 0 else '❌'
    print(f"  {'TOTAL':<{col[0]}}"
          f"{total_acq:>{col[1]},.2f}"
          f"{total_tra:>{col[2]},.2f}"
          f"{total_pl:>{col[3]},.2f}  {sign_t}")
    print("═" * W)
    print()


# ───────────────────────────────────────────────
# Entry point
# ───────────────────────────────────────────────


def print_csv(results: list[dict]):
    """Imprime el resumen en formato CSV (separador punto y coma, decimales con punto)."""
    import csv, io
    out = io.StringIO()
    w = csv.writer(out, delimiter=';')
    w.writerow(['Símbolo', 'Adquisición (USD)', 'Transmisión (USD)', 'P&L (USD)'])
    total_acq = total_tra = total_pl = 0.0
    for r in results:
        w.writerow([
            r['symbol'],
            f"{r['acquisition']:.2f}",
            f"{r['transmission']:.2f}",
            f"{r['pl']:.2f}",
        ])
        total_acq += r['acquisition']
        total_tra += r['transmission']
        total_pl  += r['pl']
    w.writerow(['TOTAL', f"{total_acq:.2f}", f"{total_tra:.2f}", f"{total_pl:.2f}"])
    print()
    print("── CSV (copia y pega en Excel; separador «;») ──────────────────────────────")
    print(out.getvalue().rstrip())
    print("────────────────────────────────────────────────────────────────────────────")
    print()

def parse_lifo_sell_args(argv: list[str]) -> set:
    """
    Parsea argumentos --lifo-sell SYMBOL:YYYYMMDD (puede repetirse).
    Úsalo cuando el XML exportado por IBKR tiene code='C;Ex' pero el HTML
    Activity Statement muestra 'C;Ex;LI' (IBKR a veces omite LI en el XML).
    Ejemplo: --lifo-sell AMZN:20251121
    Devuelve set de tuplas { ('AMZN', '20251121'), ... }
    """
    sells = set()
    i = 0
    while i < len(argv):
        if argv[i] == '--lifo-sell' and i + 1 < len(argv):
            raw = argv[i + 1]
            parts = raw.split(':')
            if len(parts) != 2:
                print(f"  \u26a0\ufe0f  --lifo-sell mal formado: '{raw}'. Formato: SYMBOL:YYYYMMDD")
                import sys; sys.exit(1)
            sells.add((parts[0], parts[1]))
            i += 2
        else:
            i += 1
    return sells


def parse_basis_args(argv: list[str]) -> dict:
    """
    Parsea argumentos --basis SYMBOL:QTY:COSTE_TOTAL (puede repetirse).
    Ejemplo: --basis CLSK:100:1689.56 --basis CLSK:200:3103.22
    Devuelve dict { 'CLSK': [(100, 1689.56), (200, 3103.22)], ... }
    El orden de los --basis determina el orden FIFO de los lotes previos.
    """
    lots: dict = {}
    i = 0
    while i < len(argv):
        if argv[i] == '--basis' and i + 1 < len(argv):
            raw = argv[i + 1]
            parts = raw.split(':')
            if len(parts) != 3:
                print(f"  ⚠️  --basis mal formado: '{raw}'. Formato: SYMBOL:QTY:COSTE_TOTAL")
                sys.exit(1)
            sym, qty_s, cost_s = parts
            try:
                qty  = float(qty_s)
                cost = float(cost_s)
            except ValueError:
                print(f"  ⚠️  --basis valores no numéricos: '{raw}'")
                sys.exit(1)
            lots.setdefault(sym, []).append((qty, cost))
            i += 2
        else:
            i += 1
    return lots


def main():
    verbose    = '--verbose' in sys.argv or '-v' in sys.argv
    force_fifo = '--fifo'    in sys.argv or '-f' in sys.argv
    files      = [a for a in sys.argv[1:]
                  if not a.startswith('-') and sys.argv[sys.argv.index(a)-1] != '--basis']
    initial_lots = parse_basis_args(sys.argv[1:])
    lifo_sells   = parse_lifo_sell_args(sys.argv[1:])

    if not files:
        print("Uso: python ibkr_pl_options.py <trades.xml> [opciones]")
        print()
        print("  -v / --verbose              Muestra detalle de cada tramo.")
        print("  -f / --fifo                 FIFO estricto (IRPF España, art. 37.2 LIRPF).")
        print("                              Sin este flag respeta el código LI de IBKR.")
        print("  --basis SYM:QTY:COSTE       Lote de compra previo al XML (puede repetirse).")
        print("                              SYM   = símbolo (ej. CLSK)")
        print("                              QTY   = número de acciones")
        print("                              COSTE = coste total del lote (sin €/$, con punto)")
        print("  --lifo-sell SYM:YYYYMMDD    Forzar LIFO en una venta específica (puede repetirse).")
        print("                              Úsalo cuando el XML omite el código LI pero el HTML")
        print("                              muestra C;Ex;LI. SYM=símbolo, fecha=tradeDate del XML.")
        print()
        print("  Ejemplos:")
        print("    python ibkr_pl_options.py trades.xml -f")
        print("    python ibkr_pl_options.py trades.xml -f --basis CLSK:100:1689.56 --basis CLSK:200:3103.22")
        print("    python ibkr_pl_options.py trades.xml --lifo-sell AMZN:20251121")
        print()
        print("  Recomendación IRPF: usar siempre -f.")
        sys.exit(1)

    mode_label = "FIFO estricto (IRPF España, art. 37.2 LIRPF)" if force_fifo else "IBKR (FIFO / LIFO según código LI)"
    print(f"\n  Modo de valoración: {mode_label}")
    if initial_lots:
        print(f"  Lotes previos inyectados: {initial_lots}")
    if lifo_sells:
        print(f"  Ventas forzadas a LIFO:   {lifo_sells}")

    trades  = parse(files[0])
    results = process(trades, force_fifo=force_fifo, initial_lots=initial_lots, lifo_sells=lifo_sells)
    print_results(results, verbose=verbose)
    print_csv(results)


if __name__ == '__main__':
    main()
