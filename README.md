# poltronesofà recliner for Home Assistant

Control poltronesofà (formerly ScS) Bluetooth power recliners from Home Assistant, without the phone app.

It works with seats that the poltronesofà Smart Sofa app controls. Their Bluetooth name starts with `ECBLE`, and they advertise manufacturer ID `0x045B`.

## What you get

Home Assistant finds each seat on its own and adds it as a device with:

- **Seat:** a cover with open (recline), close, stop and a position slider. The seat doesn't report its position, so the slider is worked out from how long the motor has run.
- **Memory 1 and Memory 2:** move to a position saved on the seat.
- **Save memory 1 and Save memory 2:** save the current position.
- **Child lock:** a switch. Its state is read from the seat.

## Install

1. Home Assistant needs a Bluetooth adapter or an ESPHome Bluetooth proxy within a few metres of the sofa.
2. In HACS, open the menu, choose **Custom repositories**, add this repository's URL, and pick **Integration** as the type.
3. Install **poltronesofà recliner** and restart Home Assistant.
4. The seats should then appear under **Settings → Devices & services** as discovered devices. If they don't, choose **Add integration** and search for "poltronesofà".

A seat takes one Bluetooth connection at a time. Home Assistant lets go of a seat 35 seconds after its last command, so the phone app can connect again after that.

## Settings

These values are at the top of `custom_components/poltronesofa/sofa.py`:

- `TRAVEL_SECONDS`: how long a full open or close takes. The default is 25. Time your own seat and change it so the slider is accurate.
- `PIN`: the Bluetooth PIN, if you've set one in the app. `0` means no PIN.

## Protocol

Every command is 8 bytes: `[counter] [group] [code, 2 bytes little-endian] [parameter, 4 bytes little-endian]`.

- Commands are written, without asking for a response, to `6e403588-b5a3-f393-e0a9-e50e24dcca9e`.
- Replies come back as notifications on `6e403589-b5a3-f393-e0a9-e50e24dcca9e`.

| Group | Code | What it does |
|---|---|---|
| `03` | `F000` | Checks the PIN (the parameter is the PIN). The seat replies `81 03 F0` if it's right, `81 04 F0` if it's wrong |
| `02` | `0337` | Asks for the child lock state. The seat replies `82 37 03 LL`, where `LL` is `01` for locked |
| `01` | `0110` / `0111` | Open / close, running until a stop |
| `01` | `0001` | Stops the motors. The app sends this three times, 20 ms apart |
| `01` | `0310` / `0312` | Saves memory 1 / 2 |
| `01` | `0311` / `0313` | Moves to memory 1 / 2, running until a stop |
| `01` | `60A3` | Switches the child lock on or off |

This was worked out from Bluetooth captures of the official app. It isn't affiliated with poltronesofà or endorsed by them.
