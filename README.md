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
   If the selected vehicle already has a running parking, nothing is bought;
   the running parking is shown in Home Assistant instead.
2. Search the configured zone code (e.g. `P4-1925`) and pick the result whose
   name contains exactly `(P4-1925)`.
3. Pick the selected vehicle by its license plate.
4. Set the end time (now + *Parking duration*) in EasyPark's *Set end time*
   dialog and verify the *Ends …* text.
5. On *Confirm parking*, wait for the price, stop on an EasyPark error banner
   (e.g. *Active session*), verify plate, zone, end time and duration, and read
   the total. The total must not exceed *Maximum price per purchase*.
6. Tap **Start Parking** (only outside test mode). EasyPark shows *Parking
   started* and a notifications prompt, which is closed (never granted).
7. The purchase counts as done only when EasyPark's *Parking session* screen
   shows a running parking for that plate. Its end time is taken from the app:
   EasyPark may adjust it to the zone's paid hours.
8. Bring the configured app back to the front.

If any check fails before *Start Parking*, nothing is paid.

If another app takes the foreground (for example a car integration polling the
same phone), the integration waits 15 seconds and starts the operation again,
up to 3 times. This applies to buying (only before *Start Parking*), ending and
reading the state. *Last result* shows `… phone busy, retry n/3` meanwhile.
After *Start Parking* a purchase is never retried: if no running parking can be found, or EasyPark
shows an error dialog, the button reports an error and *Last result* says what
the app showed.

## How ending works

*End parking* opens the running parking of the vehicle, taps *Stop*, checks
that *Stop parking?* names the same plate, confirms, and reads the final price
and period from EasyPark's *Summary*. If EasyPark answers with an error dialog,
the app is read again and the stop is tried once more.

*Read state from EasyPark* reads the running parking (end time, EasyPark's
current price) without changing anything, e.g. after a parking was started or
ended in the app by hand.

## Entities

| Entity | Purpose |
|---|---|
| Buy parking (button) | Run a purchase with the current settings; unavailable while a parking runs |
| End parking (button) | Stop the running parking of the vehicle; available only while one runs |
| Read state from EasyPark (button) | Sync the running parking from the app |
| Parking duration (number, min) | How long the next purchase lasts |
| Vehicle (select) | Which configured plate the next purchase is for |
| Test mode (switch) | Stop at *Confirm parking* instead of paying |
| Parking active (binary sensor) | A running parking is known; attributes: zone, plate, whether EasyPark adjusted the end |
| Parking started / ends (sensors) | Session times |
| Price (EasyPark) (sensor, CZK) | Price incl. service fee as EasyPark shows it; attribute `source`: `running` (Parking session screen) or `final` (Summary after ending) |
| Service fee (sensor, CZK) | EasyPark's service fee in that price |
| Zone (sensor) | Active session zone, otherwise the configured zone |
| Last result (sensor) | `dry_run_ok`, `started`, `stopped`, `active`, `no_active_parking` or `error: …`, with the quote or the stop summary as attributes |

Prices are only what EasyPark itself shows; the integration does not compute
them. They are read when parking is bought, read again (*Read state from
EasyPark*) or ended. The final price after ending, including the service fee,
replaces the running one.

The buttons follow the known state. If a parking is started or ended in the
EasyPark app by hand, press *Read state from EasyPark*; a purchase also reads
the app first and never buys a second parking for the same vehicle.

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
