# IC-7300 Web

Browser control for an Icom IC-7300 on USB: CAT control, the radio's own
panadapter + waterfall, an audio waterfall, and CW / RTTY / FT8 / FT4 decoding.

    ./run.sh            # then open http://localhost:8073

Options: `--serial /dev/ttyUSB0 --baud 115200 --civ 94 --audio <pipewire node> --host 0.0.0.0 --port 8073`
(serial port and audio device are auto-detected).

To keep it running after the terminal closes, with a memory cap, run it as a systemd user unit:

    systemd-run --user --collect --unit=cw-bot-server -p MemoryMax=4G -p MemorySwapMax=0 \
      -p WorkingDirectory=$PWD -p StandardOutput=append:$PWD/logs/server.out \
      -p StandardError=append:$PWD/logs/server.out -E PATH=$PATH $PWD/run.sh
    systemctl --user restart cw-bot-server      # or stop

## Radios
The dropdown at the top left picks the radio: **IC-7300** (CI-V), **Yaesu FTDX10** (CAT, `ftdx10.py`) or a public
**KiwiSDR** (see below).
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

## KiwiSDR (public web receivers)
**KiwiSDR** in the radio dropdown listens through a public KiwiSDR instead of a radio (`kiwisdr.py`): receive only, so
TRANSMIT is greyed out, PTT/TUNE/CW sending are refused and the robot runs only as a Dry run. Decoders, the audio
waterfall, Listen, CW spots and the NCDXF beacon monitor all work on it.
- The button beside the dropdown opens the **public receiver list** for a region (asks every receiver's status page,
  up to a minute; kept 10 min). The **recommended** one, ranked by spare slots, no time limit, then HF SNR, is
  highlighted in the accent colour; a receiver that turned us away in the last 30 min is struck through and never
  recommended. Click a row to listen. The choice and region are saved in `settings.json` (`kiwi`, `kiwi_region`).
- The receiver is streamed in-process over its websocket (`kiwi.Client`, uncompressed 12 kHz), so tuning and mode
  changes don't reconnect. Modes behave like the IC-7300: in CW the dial is the signal's frequency, heard at the 600 Hz
  pitch; RTTY puts mark on the dial at 2125 Hz. WIDE/MID/NAR = 1.2 kHz / 500 / 250 Hz in CW, 3.0 / 2.4 / 1.8 kHz in
  SSB. AGC FAST/MID/SLOW and NB are the receiver's; no preamp, attenuator or NR. The S-meter is the receiver's RSSI
  (S9 = -73 dBm).
- **Panadapter** = the receiver's own waterfall (`kiwi.Waterfall`, a second websocket opened with the audio's timestamp,
  so the receiver counts one user). Span picks the zoom that covers it (±25 kHz → 58.6 kHz wide); receivers cap the
  zoom (De Kiel: 14.6 kHz at most), and the axis shows what was actually sent. Center follows the dial; Fixed stays put
  until the dial leaves the view. Fast/Mid/Slow = Kiwi waterfall speed 4/3/2 (about 7-9 frames/s at Fast). Levels are
  mapped to the IC-7300's scope scale (-130 dBm = 0, 1.6 per dB), so Floor/Contrast work the same. Some receivers have
  fewer waterfall than audio channels (3 vs 8): if it is refused, audio carries on, the status line says so and it
  retries every 30 s.
- **One connection per IP**: close the receiver's browser tab first. Some receivers stream for ~10 s and then send
  "all N channels in use" (Hamburg did on 2026-10-28 after many test connections); the driver then backs off 60 s,
  doubling to 5 min.
- **Skimmer** (tile shown for KiwiSDR in CW / CW-R; `skimmer.py`): decodes every CW signal within ±4.8 kHz of the dial (±8.8 kHz on wideband receivers, below)
  at once. Ticking it switches the connection to I/Q (±5 kHz; the Kiwi takes the switch mid-stream), and the speaker
  and main decoder audio are then made locally from the I/Q (`kiwi.IQDemod`: same passband and pitch, ~67 dB image
  rejection). Every 0.5 s it scans the last 2 s for keyed tones; a tone found in two scans in a row gets a channel: a
  CW decoder fed the complex I/Q at its offset (no images), a 60 Hz tone filter, its own frequency-locked loop, and the
  over reader with callsign voting. Peaks that are a stronger signal's side lobes or key clicks (keyed in step with it,
  or 12 dB below what it has lately been) get no channel; channels are at least 60 Hz apart, the limit for telling two
  stations apart. A channel ignores its copy once its tone is gone (3 s) and closes after 30 s. A row shows once a
  channel has copied something: kHz, call (only once sent twice, spotted or heard before; ✓ = also spotted), WPM, SNR
  (keyed contrast), last copy. Click a row to tune there and make it the robot's target. Measured: on synthetic I/Q
  with five stations (two 70 Hz apart) every call and speed right, 1.7 % of one core; on 87 s of real 40 m I/Q from De
  Kiel the channels' copy matched a single decoder's on the same signals word for word; live, the whole process
  (receive, demod, skimmer) used ~20 % of one core. It runs in its own thread, so a busy band doesn't stall the page.
  - **Wideband receivers** (status `rx3.wf3`, 3 channels at 20.25 kHz audio; 20 of 683 public Kiwis on 2026-10-28) send
    I/Q ±10 kHz, delivered at 24 kHz. The skimmer then covers **±8.8 kHz**: two skimmers, each on a 12 kHz half
    centred 4.4 kHz below and above the dial (`kiwi.Subband`: mix, 191-tap low-pass, decimate by 2; > 60 dB rejection
    of the other half and of aliases). A signal on the seam that both halves copy is shown once. Why not ±10 kHz:
    measured on OK1XOM, the passband is flat to about ±8 kHz, +3 dB at 8–9 kHz and 8 dB down at 9–10 kHz. The
    speaker and main decoder use the middle 12 kHz (`Subband(0)`). The tile says "wideband receiver" when this is
    on. Measured: synthetic 24 kHz I/Q with stations at −8.2, −5.6, −0.06 (the seam), +4.4 and +7.7 kHz gave every
    call and speed right; live on OK1XOM 20 m, copy from 3 to 8.6 kHz below the dial; the whole process used ~28 %
    of one core (the skimmer itself ~2 %).

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
- **Side panel**: Controls, Spots and NCDXF Beacons, scrolling on their own beside the main column. The tables are
  compact there; hover a spot row or a beacon name for the hidden columns (country, km, spotters, comment...).
- **Tiles follow the mode**: CW / CW-R shows the CW decoder and the robot. Data (RTTY / RTTY-R, or any mode with the
  data flag such as USB-D) shows RTTY beside FT8 / FT4, and the robot (it transmits CW only for now: its buttons are
  disabled until RTTY and FT8 / FT4 transmit exist). SSB, AM and FM show no decoder, only the waterfalls. The server
  runs only the current mode's decoders; the enable boxes keep their setting. A running robot always stays visible.
- **CW**: auto speed tracking; raise SQL if it prints noise. **AFC** (on by default) puts the decoder on the station and keeps it there:
  every 0.25 s it looks for the tone with the most on/off contrast in the last 2 s (inside the CW filter, or ±150 Hz of
  where you put the decoder in other modes) and jumps there once two checks agree, typically 1.5-2 s after the station
  starts sending. A frequency-locked loop then measures the tone's phase during every key-down and keeps the decoder
  centred, mid-character too: on a test tone drifting 8 Hz/s it stays within 1.5 Hz (median). It ignores noise and
  carriers, holds a station for 3 s after it stops (a DX listening, the other side of a QSO) unless another is 6 dB
  stronger, and starts over after a QSY.
  **Raw / Parsed** switch on the CW tile: Raw is every character as decoded; Parsed splits the copy into overs (on
  K / KN / BK / SK / AR or 5 s of silence) and shows each one cleaned by the robot's reader (`cwparse.py`: noise
  dropped, merged words split, calls highlighted) under a line of what was sent: CQ, call → addressee, RST, name, QTH,
  73. The over still being received updates live (dimmed, "…").
  Calls in the Parsed view are repaired by voting, like forward error correction: every copy of a call in the over
  (operators send it twice or more), the same call read in earlier overs (15 min) and calls spotted within 1 kHz of
  the dial count as votes, and each letter takes the majority (DL3YFE + DL3TEE + DL3YEE -> DL3YEE; a copy that can't
  be a call, like DLEYEE, joins the real one). One copy against one is left as received unless a spot, an earlier over
  or cty.dat settles it; two real stations (OK1RR, OK1AB) are never merged, nor a call that may have lost its last
  letter (OY1C / OY1CT). Repaired calls are underlined dotted; hover for what was received. The robot reads overs with
  the same repair. On the KiwiSDR corpus copy it raised exact copies of the target's call from 84 % to 90 % (113 copies),
  with none corrected away from it. Send box uses the radio's keyer (CW mode, break-in on).
  - The decoder selector picks **Std** (threshold decoder), **HMM** (statistical decoder, better on weak and fast
    signals, prints about 2 s late) or **Both**, which shows the HMM's copy under the standard copy on the same
    audio and pitch, for live comparison. The robot uses the HMM only when HMM alone is selected. The choice is saved in `settings.json`.
  - One SQL slider sets both decoders; their levels are on the same scale (band noise alone reads about 1.5-2.2).
  - In CW mode a decoder pitch outside the radio's IF filter is refused with a warning next to the WPM readout.
  - See [docs/cw-decoders.md](docs/cw-decoders.md) for how the decoders work and how they compare.
- **RTTY**: 45.45 baud / 170 Hz; mark side follows the sideband (LSB/RTTY = mark low), tick *rev* for reversed signals.
- **FT8/FT4**: "Go to FT8 freq" sets USB-D on the current band's FT8 frequency. Needs the PC clock NTP-synced.
  Decoding uses [ft8_lib](https://github.com/kgoba/ft8_lib); "Str" is ft8_lib's score, not WSJT-X SNR.

- **Spots**: stations recently spotted anywhere in the band, all modes, from [DX Summit](http://www.dxsummit.fi), [HamQTH](https://www.hamqth.com)
  and the [Reverse Beacon Network](https://reversebeacon.net) (CW and RTTY, spotter SNR/WPM). RBN logs in with your callsign
  (robot Callsign field, or `--rbn-call`). Cluster spots carry no mode, so it is read from the comment (FT8, RTTY, SSB, PSK…),
  else from the band plan: FT8/FT4 windows, CW segment, data, phone. The Mode dropdown filters the list. Click a row to
  tune the radio (or the KiwiSDR) there in that mode: CW also becomes the robot's Answer target; SSB picks LSB below
  10 MHz (USB on 60 m); RTTY tunes the mark; FT8/FT4 go to USB-D on the standard dial frequency, other data to USB-D
  1.5 kHz below the spot. Also available as `GET /api/spots?band=20&max_age=900&mode=CW`.
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

## Robot
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
  - **Hunt with the skimmer** (KiwiSDR, skimmer ticked): Hunt listens to every skimmer channel, not just the main
    decoder. Stations calling CQ (a CQ in an over whose call is sure, the tone still there, within 20 s; a CQ that
    turned into a contact is dropped) are listed in the robot panel: kHz, call, country, dB, WPM, and why one can't
    be answered (CQ EU from Asia, DX-only hunt, tried with no answer in the last 5 min). The strongest answerable
    one is picked: the radio is tuned onto it (dial on its carrier, main decoder and AFC on the pitch). If the CQ is
    still going, Hunt waits for it to end, heard by the main decoder (CQ and the call), or the main decoder hears
    the tail and the skimmer saw the CQ over close. Then it calls twice, and goes to the next station if there is
    no reply. Live, dry run, 40 m on OK1XOM: it listed GM0GMN, W0EAS and M0KTZ, tuned to and answered GM0GMN
    (39 dB, the strongest), then moved on down the list; it declined one channel where the main decoder heard a
    different station (OK1DOC/P) from the one the skimmer had named.
  Only the robot's modes answer: while idle it does not react to anything the decoder prints.
- Each received over is read first by a rule-based reader (`cwparse.py`: who is calling, their RST, name, QTH,
  repeat requests, 73), which takes well under a millisecond. It handles merged doubles (IGORIGOR) and votes out
  decoder errors in repeated words. The model is asked only when the reader is unsure, about 5 s per over: a
  question to answer (RIG? WX?) or many unknown words. If the model is unreachable the reader's result is used.
  The robot log shows READ for the reader and LLM for the model. The code sends standard messages, handles turn-taking,
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
python3 kiwi.py record HOST:8073 7024.5 60 --iq # one recording to WAV: USB audio, or raw I/Q with --iq
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
- **I/Q** (`capture --iq`): each dwell also keeps the receiver's raw I/Q, `audio/*.iq`, 12 kHz s16le interleaved I, Q,
  -5..+5 kHz around the dial (about 173 MB per hour). The `.raw` is then demodulated from it (`kiwi.IQToUSB`, complex
  band-pass 300-2700 Hz, 76 dB image rejection), so `cwtune.py` works the same. `kiwi.py record --iq` writes a stereo
  WAV (left I, right Q).
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

Files: `radio.py` (IC-7300 CI-V), `ftdx10.py` (FTDX10 CAT), `rig.py` (radio detection, port + audio pairing), `clock.py` (NTP check), `decoders.py` (DSP), `cwhmm.py` (HMM CW decoder), `robot.py` (CW robot), `spots.py` (DX spots),
`pathcheck.py` (who heard us), `beacons.py` (NCDXF beacon monitor), `beaconlog.py` + `spaceweather.py` (its JSONL propagation log), `geo.py` (cty.dat prefixes, grid/distance; `vendor/cty.dat`), `cwparse.py` (rule-based over reader), `kiwi.py` + `corpus.py` + `cwtune.py` (KiwiSDR capture and decoder tuning), `kiwisdr.py` (KiwiSDR as a receive-only radio), `skimmer.py` (CW skimmer over its I/Q), `server.py`
(aiohttp + websocket), `static/` (UI).
