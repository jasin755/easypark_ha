# EasyPark (Android companion) for Home Assistant

Buys EasyPark street parking from Home Assistant by driving the official
EasyPark Android app on a dedicated companion phone.

EasyPark has no public API. This integration does not log in to EasyPark,
intercept traffic or reimplement its requests. The genuine app stays signed in
on the phone, and a small generic Android AccessibilityService (the
*VAG Companion Agent*, 0.8.0+) reads the visible screen and taps on request.

> [!WARNING]
> This spends real money. **Test mode** is on after installation: the purchase
> runs up to EasyPark's *Confirm parking* screen, reads the price back and
> returns without paying. Turn it off only after a few successful test runs.

## How a purchase works

1. Open EasyPark (from any state, including a cold start) and return to the map.
2. Search the configured zone code (e.g. `P4-1925`) and pick the result whose
   name contains exactly `(P4-1925)`.
3. Pick the selected vehicle by its license plate.
4. Set the end time (now + *Parking duration*) in EasyPark's *Set end time*
   dialog and verify the *Ends …* text.
5. On *Confirm parking*, verify plate, zone, end time and duration, and read the
   total price. The total must not exceed *Maximum price per purchase*.
6. Tap **Start Parking** (only outside test mode), then bring the configured app
   back to the front.

If any check fails, nothing is paid. If another app takes the foreground
before *Start Parking* (for example a car integration polling the same phone),
the purchase is retried once after 30 seconds. After *Start Parking* has been
tapped it is never retried.

## Entities

| Entity | Purpose |
|---|---|
| Buy parking (button) | Run a purchase with the current settings |
| End parking (button) | Not implemented yet |
| Parking duration (number, min) | How long the next purchase lasts |
| Hourly rate (number, CZK/h) | Your rate, used for *Cost so far* |
| Vehicle (select) | Which configured plate the next purchase is for |
| Test mode (switch) | Stop at *Confirm parking* instead of paying |
| Parking active (binary sensor) | A session bought here has not ended yet |
| Parking started / ends (sensors) | Session times |
| Cost so far (sensor, CZK) | Hourly rate × elapsed time of the running session |
| Quoted total (sensor, CZK) | Total from *Confirm parking*, incl. service fee |
| Zone (sensor) | Active session zone, otherwise the configured zone |
| Last result (sensor) | `dry_run_ok`, `started`, `running` or `error: …`, with the read-back quote as attributes |

## Requirements

- Android phone with EasyPark installed, signed in, payment method and the
  vehicles added. The app language must be **English** (screen texts are read).
- VAG Companion Agent 0.8.0+ (adds `/set-text` and launching other apps).
  The phone needs a fixed IP address; the Agent's LAN API listens on port 8765.
- Home Assistant must reach the phone on the LAN.

## Configuration

*Settings → Devices & services → Add integration → EasyPark (Android companion)*

- phone IP, Agent port and Agent token;
- parking zone code, e.g. `P4-1925`;
- license plates, comma separated (each must exist in EasyPark);
- maximum price per purchase;
- the app to bring back to the front afterwards (default
  `com.volkswagen.weconnect`; empty = stay in EasyPark).

Everything, including the phone IP address, port and token, can be changed
later under *Configure*; the connection is checked before it is saved.

## Tested with

EasyPark 26.40.1 (English), Pixel 4a / Android 13, VAG Companion Agent 0.8.0.
App updates can change the screens; the integration then stops with an error
instead of guessing.
