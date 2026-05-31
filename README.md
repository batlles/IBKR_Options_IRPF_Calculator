# IBKR_Options_IRPF_Calculator
IBKR Stock P&amp;L Calculator para IRPF España

Lógica:
  1. Lee todos los TradeConfirm del XML exportado desde IBKR (FlexQuery o Activity Statement).
  2. Para cada subyacente, localiza los movimientos STK y las opciones que los causaron.
  3. Ajusta el valor de adquisición/transmisión con las primas netas de las opciones.
  4. Aplica FIFO (por defecto IBKR; con -f, FIFO estricto según art. 37.2 LIRPF).
  5. Imprime tabla resumen: subyacente | adquisición | transmisión | P&L + CSV para Excel.

Uso básico:
    python ibkr_pl.py <trades.xml> [opciones]

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
    python ibkr_pl.py mi_export_2025.xml -f

    # Con detalle de cada tramo
    python ibkr_pl.py mi_export_2025.xml -f -v

    # CLSK / KO: acciones compradas antes del período del XML
    # (el script avisará con ⚠️ y te dirá qué símbolo necesita --basis)
    # El COSTE lo encuentras en la columna "Basis" del HTML resumen de IBKR
    python ibkr_pl.py mi_export_2025.xml -f \
        --basis CLSK:100:1689.56 \
        --basis CLSK:200:3103.22 \
        --basis KO:100:6575.81

    # AMZN: el XML exportado omite el código LI en una venta C;Ex;LI
    # (sin --lifo-sell el resultado IBKR difiere; para IRPF usa -f igualmente)
    python ibkr_pl.py mi_export_2025.xml --lifo-sell AMZN:20251121

    # Combinando varias opciones
    python ibkr_pl.py mi_export_2025.xml -f \
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
