# inventree-smallsmt-stock

InvenTree plugin that imports **pick-and-place (SMT) feeder stock** from the machine's
proprietary `config_feed.fig` file into InvenTree stock.

## What it does
On a schedule (and on-demand at `/plugin/smallsmt-stock/run`):
1. Reads `config_feed.fig` from an SMB share (`smbprotocol`, credentials in settings).
2. Parses the feeder table (bundled `smt_parser.py`) → per feeder: `part_value` + `count_number`.
3. Resolves `part_value` to an InvenTree part — exact match by **Part name** (generic parts like
   `C-100nF-10%-50V-0603-X7R`), then **ManufacturerPart MPN** (e.g. `CSD17578Q5AT`), then **IPN**.
4. Reconciles that part's stock at a dedicated **`SMT Feeders`** location to the feeder count
   (creates/updates a single StockItem; stock at other locations is untouched).
5. Reports unmatched feeder values (placeholder entries like `R-0R-x%-0402-xmW`, or parts not in
   the library) so they can be fixed manually.

Validated against a real `config_feed.fig`: 32 of 44 counted feeders resolve automatically.

## Install
InvenTree Admin → Plugins → install from the GitHub URL + `inventree-smallsmt-stock`
(pip pulls `smbprotocol`). Enable the plugin plus **Schedule** and **URL** integration.
Restart the server + worker after enabling so both registries load it.

## Configure (plugin Settings)
`IMPORT_ENABLED`, `STOCK_LOCATION` (default `SMT Feeders`), `FIG_PATH` (path to config_feed.fig on
the share), and SMB `HOST/SHARE/USER/PASSWORD/DOMAIN`.

## Test
```
curl -H "Authorization: Token <token>" https://<inventree>/plugin/smallsmt-stock/run
```
Returns `{matched, created, updated, unchanged, unmatched:[...]}` without waiting for the schedule.

## Notes
- The offer feed (separate `inventree-altium-bridge` plugin) reports `total_stock`, which now
  includes this SMT-location stock — so Altium tiles reflect real feeder quantities.
- Matching is exact. If MPN formatting differs (e.g. `74LVC1G125GW,125` vs `74LVC1G125GW125`),
  the value lands in `unmatched`; a normalized-MPN fallback can be added if needed.
