# RECORA — lite screen recorder (Windows)

CPU / RAM ගොඩක් අඩුවෙන් screen එක record කරන simple recorder එකක්. JARVIS demo වගේ දේවල් record කරද්දී voice එක shake වෙන්නේ නැති වෙන්න හදපු එකක්.

## ඇයි අඩුවෙන් load වෙන්නේ

- Screen එක **GPU එකෙන්ම** capture කරනවා (Windows Desktop Duplication — FFmpeg `ddagrab`).
- Encode කරන්නෙත් GPU එකේ video engine එකෙන් — Intel Quick Sync / NVIDIA NVENC / AMD AMF. Frames CPU එක හරහා යන්නේ නෑ.
- FFmpeg run වෙන්නේ **below-normal priority** එකෙන් — CPU එක ගැන තරඟයක් ආවොත් JARVIS ම දිනනවා.
- System sound එක WASAPI loopback එකෙන් කෙලින්ම WAV එකකට — playback එකට කිසිම බලපෑමක් නෑ.
- Video + sound එකතු කරන්නේ Stop කළාට පස්සේ විතරයි.

## .exe එක හදාගන්න

### විදිහ 1 — GitHub Actions (නිර්දේශිතයි)
1. GitHub එකේ අලුත් repo එකක් හදලා (උදා: `recora`) මේ folder එකේ files ඔක්කොම upload කරන්න (`.github` folder එකත් එක්ක).
2. **Actions** tab → **Build RECORA (Windows)** → run වෙලා ඉවර වෙනකම් ඉන්න (~3-5 min).
3. Run එක ඇතුළේ **Artifacts** → `RECORA-windows` download කරලා extract කරන්න.
4. `RECORA.exe` run කරන්න. (`ffmpeg.exe` එකත් ඒ folder එකේම තියෙන්න ඕන.)

### විදිහ 2 — Laptop එකේම
`build.bat` double-click කරන්න. Python packages දාලා, FFmpeg download කරලා, `dist\RECORA\RECORA.exe` හදනවා. (FFmpeg ~100 MB download එකක් තියෙනවා.)

### Build නොකර run කරන්න
```
pip install pyaudiowpatch
python recora.py
```
`ffmpeg.exe` එක `recora.py` එක තියෙන folder එකේම දාන්න.

## පාවිච්චි කරන හැටි

- **F9** = Start / Stop (window එක hide වෙලා තිබුණත් වැඩ).
- Start කළාම 3-2-1 countdown එකක් දීලා window එක minimize වෙනවා — recording එකට එන්නේ නෑ.
- **System sound** = JARVIS voice එක. **Microphone** = ඔයාගේ කටහඬ (JARVIS එක්ක කතා කරන demo එකකට on කරන්න).
- Recordings save වෙන්නේ `Videos\RECORA` එකට (`Change` එකෙන් වෙනස් කරන්න පුළුවන්). File name: `RECORA_YYYY-MM-DD_HH-MM-SS.mp4`.
- පළමු වතාවට open කළාම තත්පර කීපයක් GPU encoder එක check කරනවා. යටින් **Encoder:** line එකේ කොළ පාටින් `(GPU) ✓` පෙන්නුවොත් හරි.

## ප්‍රශ්න ආවොත්

| ප්‍රශ්නය | කරන්න ඕන දේ |
|---|---|
| Encoder එක `CPU x264` (කහ පාට) | GPU encoder වැඩ කළේ නෑ. Intel graphics driver එක update කරලා `Re-check` ඔබන්න. Encoder line එක click කළාම විස්තර පෙන්නනවා. |
| Sound නෑ | Windows එකේ default speaker එක තමයි record වෙන්නේ. JARVIS play වෙන්නේ ඒ device එකෙන්ද බලන්න. |
| Sound ටිකක් ඉස්සරහින් / පස්සෙන් | `%APPDATA%\RECORA\config.json` එකේ `audio_offset_ms` වෙනස් කරන්න (උදා: `150` = sound 0.15s පස්සට, `-150` = ඉස්සරහට). |
| Mic sound එක අඩුයි | `config.json` එකේ `mic_volume` වැඩි කරන්න (උදා: `1.8`). |
| F9 වැඩ නෑ | වෙන app එකක් F9 අරන්. Button එකෙන් Start/Stop කරන්න. |

## JARVIS voice එක තවත් smooth කරගන්න tips
- Xbox Game Bar එකේ **"Record in the background"** off කරන්න (Settings → Gaming → Captures).
- Record කරද්දී laptop එක charger එකේ, **Best performance** power mode එකේ තියන්න.
- Sound settings → **Communications** tab → **Do nothing** (mic එක use වෙද්දී අනිත් sound අඩු කරන එක නවත්තන්න).
