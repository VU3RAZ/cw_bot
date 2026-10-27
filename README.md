# IC-7300 Web

Browser control for an Icom IC-7300 on USB: CAT control, the radio's own
panadapter + waterfall, an audio waterfall, and CW / RTTY / FT8 / FT4 decoding.

    ./run.sh            # then open http://localhost:8073

Options: `--serial /dev/ttyUSB0 --baud 115200 --civ 94 --audio <pipewire node> --host 0.0.0.0 --port 8073`
(serial port and audio device are auto-detected).

## Radios
The dropdown at the top left picks the radio: **IC-7300** (CI-V) or **Yaesu FTDX10** (CAT, `ftdx10.py`).
Radios are **detected by their USB ids** at startup and every 2 s while running (`rig.detect()`): IC-7300 = CP2102
`10c4:ea60` whose USB serial number Icom sets to "IC-7300 …"; FTDX10 = CP2105 `10c4:ea70`, interface 0 (Enhanced port,
CAT; other Yaesu rigs use the CP2105 too, so the driver also checks the CAT ID answer `ID0761`). The dropdown marks
radios that are not plugged in "(not detected)" and a toast says when one is plugged in or removed. At startup the last
used radio is chosen if plugged in, else any plugged-in radio (`--radio` overrides); the choice is saved in
`settings.json`. If the radio in use is unplugged and another one is plugged in, the server switches to it (not while the
CW robot or beacon monitor runs - it says so instead); a radio that comes back on another port is followed. Its receive audio is the USB sound card on the same USB hub as that
port (both radios use a TI "USB Audio CODEC", so the name alone can't tell them apart). Port overrides:
`settings.json` → `{"ports": {"FTDX10": "/dev/ttyUSB1"}}`.

FTDX10 notes (written from the FTDX10 CAT manual; tested against a simulated radio, **not yet on the real FTDX10**):
- CAT baud: tries 38400, 19200, 9600, 4800 (menu CAT RATE); RTS is asserted for menu CAT RTS = ENABLE.
- No spectrum scope over CAT: the panadapter is empty (use the audio waterfall).
- Filters are widths (SH): WIDE / MID / NAR = 1.2 kHz / 500 Hz / 250 Hz in CW/RTTY, 3.0 / 2.4 / 1.8 kHz in SSB.
- CW sending: the FTDX10 has no free-text CW command, so text is loaded into keyer memory 1 (`KM1`, ≤ 50 characters)
  and played (`KY1`) chunk by chunk; prosigns like `<KN>` go out as two letters.
- S-meter / SWR use Hamlib's Yaesu calibration (approximate).
- CW is CW-U (CW-R = CW-L), RTTY is RTTY-L (RTTY-R = RTTY-U), USB/LSB + DATA = DATA-U/DATA-L.

## PC clock
The beacon monitor (10 s slots) and FT8/FT4 need the PC clock NTP-synchronised. The server checks it at startup and
every minute (`clock.py`: chrony's offset and error bound when chrony runs, else systemd's NTPSynchronized) and shows it
in the header (**NTP ✓ 0.0 ms**, red **NTP ✗** when not synced or more than 100 ms off; hover for details). The beacon
monitor refuses to start without it, and every beacon log record carries `pc_clock` (synced, offset, error bound,
reference).

## IC-7300 settings (MENU > SET > Connectors)
- CI-V USB Baud Rate: 115200 (or Auto), CI-V Address 94h, CI-V Transceive ON
- USB SEND / USB Keying: OFF (DTR/RTS are held low anyway)
- For FT8/RTTY over USB audio: DATA MOD = USB, ACC/USB AF output level ~50%

## Using it
- **Panadapter**: click to tune, mouse wheel steps by the Step setting; span / center-fixed / sweep speed set the radio scope.
- **Frequency**: mouse wheel over a digit tunes that digit; click it to type (kHz or MHz). Arrow keys step.
- **Audio waterfall**: click a signal to point the CW (snaps to the peak) or RTTY decoder at it.
- **CW**: auto speed tracking; raise SQL if it prints noise. Send box uses the radio's keyer (CW mode, break-in on).
- **RTTY**: 45.45 baud / 170 Hz; mark side follows the sideband (LSB/RTTY = mark low), tick *rev* for reversed signals.
- **FT8/FT4**: "Go to FT8 freq" sets USB-D on the current band's FT8 frequency. Needs the PC clock NTP-synced.
  Decoding uses [ft8_lib](https://github.com/kgoba/ft8_lib); "Str" is ft8_lib's score, not WSJT-X SNR.

- **CW Spots**: stations recently spotted on CW in the band's CW segment, from [DX Summit](http://www.dxsummit.fi), [HamQTH](https://www.hamqth.com)
  and the [Reverse Beacon Network](https://reversebeacon.net) (spotter SNR/WPM). RBN logs in with your callsign
  (robot Callsign field, or `--rbn-call`). Click a row to switch to CW, tune to the station, and make it the robot's
  Answer target. Also available as `GET /api/spots?band=20&max_age=900`.
- **Path check** (robot panel): tune into a band's CW segment, then the robot sends `QRL?`, listens, and calls CQ 3 times
  (answers are worked as a normal QSO). Who heard you is collected live from RBN and, once at the end (~4 min),
  from PSKReporter (max one query per 5 min): receiver, country, distance/bearing from your Grid (or your callsign's
  country), SNR. For the next hour, CW Spots on that band marks with ✓ (and lists first) stations spotted by a
  spotter within 2000 km of a receiver that heard you - i.e. on a path that is open for you.
  Honors Dry run (goes through the motions without keying; nothing will be heard).
  Receivers reporting to both networks are shown once ("Heard via RBN + PSKReporter"); grids learned from
  PSKReporter also place RBN spotters. The last result is saved to `logs/pathcheck_last.json` and reloaded on restart.
- **NCDXF Beacons**: monitors the [NCDXF/IARU beacons](https://www.ncdxf.org/beacon/) (18 beacons, 14.100 / 18.110 /
  21.150 / 24.930 / 28.200, 10 s slots in a 3-minute cycle from 00:00 UTC; callsign at 22 WPM then four 1 s dashes at
  100 W, 10 W, 1 W, 0.1 W). Modes: **Cycle all bands** (hops band every slot; all 90 beacon/band pairs in 15 min),
  **One band** (all 18 beacons every 3 min), **Follow one beacon** (up through the bands with it). Receive only; the radio
  is put in CW with FIL2 (500 Hz here; FIL3 is set to 50 Hz, too narrow for beacons that arrive up to ±50 Hz off
  the pitch), retuned 0.4 s before each slot, and freq/mode/filter are restored on Stop. Needs the PC clock NTP-synced.
  Each slot is measured from the audio: the beacon's own callsign keying finds its exact timing and tone (±60 Hz of the
  CW pitch) and must stand out by 6 dB (identifies it, rejects carriers/other stations); each dash's S/N is measured in a
  30 Hz track against the noise in the beacon's key-up gaps. A dash counts as heard at S/N ≥ 0 dB; the matrix shows how
  many dashes were heard (100 W down) and the 100 W S/N. On synthetic audio: no false detections in 500 noise slots,
  beacons recognised down to ~6 dB below the noise in a 500 Hz bandwidth. AGC compresses the dB values but not the
  heard/not-heard steps. Every slot is logged to `logs/beacons.csv`. A propagation log for later analysis (e.g. by an LLM) goes to `logs/ncdxf/`:
  `slots-YYYY-MM-DD.jsonl` (every slot with path geometry, sun elevation at both ends and midpoint, receiver settings,
  NOAA space weather: SFI, Kp, X-ray class, R/S/G) and `passes-YYYY-MM-DD.jsonl` (a summary per 15-minute UTC window,
  with a plain-English `summary`); field reference in `docs/ncdxf-log-format.md`. Can't run together with the CW robot. While it runs it owns the radio: the server refuses frequency/mode changes, PTT, TUNE
  and CW sending from every browser (the UI greys the tuning controls and says why), and 1.5 s into each slot it checks
  the frequency the radio reports, retunes and skips that slot if a tuning command was dropped. The radio's own VFO knob
  can't be locked from here; the next slot retunes it.
- **Layout**: every panel has a ▾ button (and the Band/Mode/Receive/Transmit groups fold by their title) to collapse it;
  the state is remembered per browser. The Controls column stays in view while scrolling.
- **Listen**: the 🔈 button in the header streams the receiver audio (12 kHz) to the browser.

## CW robot
Makes a complete CW contact by itself using the local gpt-oss model on llama.cpp
(`http://127.0.0.1:8081`, its `/completion` endpoint).

- **My station**: your call / name / QTH (grid optional, used for path-check distances), keyer WPM, max CQs.
- **Other station**: the station you are working. During a contact Call / RST sent / RST rcvd / Name / QTH fill in
  from the decoder (marked *decoded*); type in a field to correct it (marked *you*) and the robot uses and logs your
  value from then on - later decodes no longer change it. Before **Answer**, put the call here (or click a spot);
  anything else you already know (e.g. their name) is used as a starting value. RST sent left empty = from the S-meter.
- **Call CQ**, or **Answer** the station in Other station / Call, or **Hunt**: listen on the frequency (up to 30 min)
  and answer the first CQ heard. The call is taken from the copy in code (sent twice or after DE, valid prefix);
  CQ DX from your own country and CQ EU/NA/JA... not meant for you are skipped, as is a station that does not come back.
  The selector next to Hunt picks **All stations** or **DX only** (callers in other countries).
  Only the robot's modes answer: while idle it does not react to anything the decoder prints.
- The model reads each received over from the CW decoder (who is calling, their RST, name,
  QTH, repeat requests, 73). The code sends standard messages, handles turn-taking,
  retries (AGN? / QRZ?), ignores overs of another contact on the frequency ("UA7N DE VU2TMP":
  a callsign next to DE that is neither ours nor our partner's), and stops after max CQs, 3 unanswered overs, or 15 minutes.
- **Dry run** (on by default) goes through the whole contact but never keys the radio;
  use *Inject RX* to type what the other station "sent" and watch the robot respond.
- Live mode puts the rig in CW, sets semi break-in if it was off, sets the keyer speed and
  sends through the radio's keyer. **STOP** (or Esc) cancels and unkeys at once.
  You remain the control operator: stay at the station while it runs.
- Log: `logs/cw_robot.txt` (each contact with full transcript) and `logs/cw_robot.adi`
  (ADIF, contacts where reports were exchanged). Dry runs go to `logs/cw_robot_dryrun.*`.

## Real CW traffic and decoder tuning
The CW decoder is tuned on real signals recorded from public KiwiSDRs, never from your own radio.
Retune it whenever the decoder changes or copy looks worse than it should:

```sh
python3 kiwi.py europe                          # usable KiwiSDRs in a region, best first
python3 corpus.py capture --call N0CALL --minutes 60
python3 corpus.py stats                         # how much audio / readable copy there is
python3 cwtune.py --dry-run                     # search and report; drop --dry-run to save
```

- **Receivers** come from ReceiverBook's public KiwiSDR list (cached a day in `logs/kiwi_receivers.json`). They are ranked
  by spare slots, no time limit, then HF SNR. Regions: europe (densest CW traffic, default), na-east, japan, india, oceania.
  `--kiwi host:port` uses one receiver. A KiwiSDR allows **one connection per IP**: close its browser tab first.
- **Stations**: capture listens to stations that **RBN spotters within 800 km of the receiver** hear loudly. The RBN login
  needs `--call`. That is measured evidence they are readable there; worldwide spotter counts are not. Without one it
  falls back to DX-cluster spots within 3000 km. It stays 8 min per frequency (2 min if nothing readable comes through),
  listens in a 300-2700 Hz passband and follows the strongest keyed tone.
- **Corpus**, in `logs/corpus/`: `audio/*.raw` (12 kHz mono s16le) and a `.json` per recording (receiver, dial, target
  spot, pitch track), plus `copy-*.jsonl` with the decoded copy. Raw audio is about 86 MB per hour.
- **Tuning** (`cwtune.py`): it fits the tone filter width, squelch, envelope smoothing, glitch length, key hysteresis and dash/character/word gap lengths
  by replaying the recorded audio.
  - Copy is scored for readability: QSO words, valid calls and reports count; `*` characters and merged words cost.
  - Overs you type into `logs/corpus/labels.jsonl` (`{"audio", "start_s", "end_s", "text"}`) are scored by exact
    words and count most.
  - A result is saved to `decoder_params.json` only if it scores better, the synthetic suite is no worse, and
    `tests/` passes. The tests guard short word spacing, for example.
  - Restart the server to use the new values.
- `kiwiclient` (github.com/jks-prv/kiwiclient) is cloned into `vendor/` on first use.
- Tests: `python3 -m pytest -q tests`. Synthetic CW comes from `cwsynth.py`.

Files: `radio.py` (IC-7300 CI-V), `ftdx10.py` (FTDX10 CAT), `rig.py` (radio detection, port + audio pairing), `clock.py` (NTP check), `decoders.py` (DSP), `robot.py` (CW robot), `spots.py` (DX spots),
`pathcheck.py` (who heard us), `beacons.py` (NCDXF beacon monitor), `beaconlog.py` + `spaceweather.py` (its JSONL propagation log), `geo.py` (cty.dat prefixes, grid/distance; `vendor/cty.dat`), `cwparse.py` (rule-based over reader), `kiwi.py` + `corpus.py` + `cwtune.py` (KiwiSDR capture and decoder tuning), `server.py`
(aiohttp + websocket), `static/` (UI).
