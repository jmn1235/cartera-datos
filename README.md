# cartera-datos

Precios de mercado para la app **Cartera JM**, bajados por un script (sin IA) con GitHub Actions.

- `tickers.json`: tickers con historial completo (BYMA por tipo y EE.UU.). Si comprás un activo nuevo, agregalo acá.
- `scripts/fetch.py`: baja data912 (BYMA en vivo e historial), argentinadatos (dólar MEP y CCL) y Yahoo Finance (EE.UU. y benchmarks). Corre los días hábiles a las 12:10, 15:10 y 18:10 (hora de Buenos Aires) y a mano desde **Actions → Precios de mercado → Run workflow**.
- `scripts/sync.py`: lo usa el Actualizador de la app para pasar estos datos a la base, sin transcribir números.
- `data/`: salida (`last.json`, `hist/`, `us/`, `fx.json`, `status.json`).

Solo contiene tickers y precios públicos: ni cantidades ni montos de la cartera.
