<p align="center"><img src="public/favicon.svg" width="96" height="96" alt="DiscDock logo"></p>

# DiscDock

DiscDock turns a Windows PC with an optical drive into an automatic ripping station. Insert a disc and DiscDock identifies it, rips it and puts the result in a library folder. It runs natively on Windows, without Docker, WSL or a virtual machine.

DiscDock is inspired by [Automatic Ripping Machine](https://github.com/automatic-ripping-machine/automatic-ripping-machine) and AI Generated from scratch for Windows.

## How it works

<p align="center"><img src="assets/discdock-flow.svg" width="100%" alt="How DiscDock works: a disc is inserted and identified. Movies and series are ripped with MakeMKV, with a rescue rip for damaged discs; audio CDs are ripped to FLAC with cyanrip while the album is found on MusicBrainz; games and other discs have their files listed and are copied whole into a checked image. Every rip is verified and the disc is ejected. Meanwhile movies can be transcoded or repaired and CD tracks are named and tagged, then everything moves into the library and you are notified."></p>

## Features

- **DVDs and Blu-rays** are inspected and ripped with MakeMKV. DiscDock picks the main feature, or you choose the titles yourself; *Always choose titles* in Settings holds every disc on the dashboard for that choice, listing the titles between the minimum and maximum length. HandBrake conversion to smaller files is optional.
- **Audio CDs** are ripped with cyanrip. Enter your drive's read offset in Settings, from the AccurateRip drive offset list, so rips can be checked against AccurateRip. While the CD rips, DiscDock looks the album up on MusicBrainz. When MusicBrainz knows several releases of the CD, you choose yours on the dashboard. When the lookup finds the wrong album or none, *Find the album* searches MusicBrainz by name or by the barcode on the back of the case, typed or scanned with a camera. For a CD MusicBrainz does not know, you type the artist and album, photograph the back of the case to read the track names with OCR (English and Norwegian, in the browser), and photograph the front for the cover. The photos and what you typed are kept, so you can open the form again to change something. When the rip is done, the tracks get the album's names, tags and cover. If MusicBrainz is busy or unreachable at that point, or you ticked *Keep in staging until the album is found*, the tracks wait in staging instead of going into the library without names, and the next CD can go in meanwhile; once the album is found or entered, *Finish* names, tags and moves them. An album you enter yourself always finishes the CD. If MusicBrainz has no cover for the album, photograph the front of the case or choose a picture on the dashboard. Before a photo becomes the cover, an editor lets you drag its corners onto the cover to crop it and straighten a photo taken at an angle, turn or mirror it, and adjust brightness, contrast and saturation. The photo as taken is kept until the tracks are tagged, so the cover can be edited again. DiscDock tells CDs apart by their table of contents, so inserting another CD never continues the job of an earlier one. DiscDock asks MusicBrainz once per CD and waits a few seconds between requests. MusicBrainz never stops a rip: when it is busy (it answers 503 to more than about one request per second), the dashboard says so, and you can search again a moment later.
- **Games, programs and any other disc** are backed up as an image of the whole disc, filed under *other*. DiscDock lists everything on the disc first so you can see what it is and name it. See below.
- **Metadata** comes from OMDb, with a search for discs whose label is unclear.
- **Damaged discs** can still be ripped. See below.
- **A dashboard** in your browser shows drives, progress, job history, logs and your library. Notifications go through Apprise (Discord, Telegram, email and many more).
- **Several drives** can work at the same time. Jobs survive a restart or a disconnected drive and can be retried.
- **Discs ripped before** are recognised. You can rip one again as a new copy, or add more of its titles to the folder it was completed in, for example the other episodes of a series first ripped as a movie. The titles ripped last time are left unticked, new files are numbered after the ones already there, and nothing in the folder is replaced.

## Games, programs and other discs

A disc that is not a film or an album — a PC game, an installer, a folder of photos, anything — is backed up as an image of the whole disc, exactly as it is. Nothing is converted, so the backup plays and installs the way the disc does.

**The disc is shown to you first.** A volume label like `SIMS2_EP1` tells you little, so DiscDock lists what the disc holds before it copies anything: every file with its size, the folders they sit in, and a reading of what the disc is for (a game or program that starts itself, software to install, music, pictures, documents, or plain files). The dashboard shows that list with a search box, suggests a name from the label, and waits. Nothing is backed up until you name it.

**The backup is the disc itself.** DiscDock copies every sector into an ISO image, so a game keeps its installer, its data files and its folder layout. Windows opens an image with a double-click: it appears as a drive, and the game installs or runs from it as it would from the real disc. Next to the image DiscDock writes two files: *Disc contents*, listing every file in the backup so you can search it without opening the image, and *How to use this backup*, which says how to attach it and play it.

**The backup is checked against the disc.** When the image is written, DiscDock reads the image's own file system back and compares it with the files the disc showed: every file, at the same size. A truncated or unreadable image is rejected rather than filed as a good backup. A disc whose file system Windows cannot read at all is still copied; only the file-by-file check is skipped, and the job says so.

Backups are filed under `other` in your library folder, named after what you called them.

Some game discs carry copy protection that looks for the original disc in a drive. Those refuse to run from any backup. DiscDock copies discs; it does not remove protection.

## Damaged discs

When MakeMKV reaches a scratched part of a DVD or Blu-ray, it can retry for hours. DiscDock reads such a disc itself and then works through every way of getting the movie out of what it read, on its own, so a damaged disc is not left waiting for you to press anything.

**Reading the disc.** DiscDock finds the movie in the disc's file system and reads only the movie and the navigation data MakeMKV needs, so extras and menus cost no time. When a block cannot be read, DiscDock skips ahead to the next moment of video, as a player does, and comes back later to retry the skipped spots. It stops retrying when that no longer recovers anything, or when you choose *Skip retrying*. Everything that could be read is copied exactly, with no loss of quality. A rescue that was interrupted continues where it stopped, even after a restart or in another job for the same disc. If the drive stops responding, unplug it and plug it back in; DiscDock picks the job up again when the disc is back. A job you stopped, or one that failed, is not restarted just because DiscDock starts with the disc already in the drive.

**Getting the movie out, fastest way first.** **MakeMKV** comes first: it is the quickest and keeps the most, with the chapters and every track. When the damage leaves the disc's navigation in a state MakeMKV refuses — it then calls the movie a fake title, or finds no title at all — **FFmpeg** copies the movie's own sectors straight out of the image instead, in about a minute, ignoring that navigation. It skips what it cannot use, so damage becomes a short gap; the video and audio are copied untouched, without chapters. A DVD image that is still scrambled can only be read by a player that decrypts DVDs, so FFmpeg is skipped for it. If neither manages and the disc is still in the drive, DiscDock reads the rest of the disc, or the spots it skipped, and tries again. **VLC** plays the movie out of the image as a last resort, because it is the slowest by far; if it stops writing for ten minutes, DiscDock ends it rather than waiting for hours. Only when every one of them has failed does the job stop, and it then says what each one ran into.

**When MakeMKV lists no titles at all**, DiscDock reads the titles from the disc's own tables — the same tables a player follows — and carries on with those. A disc MakeMKV cannot open is therefore not the end of the road. Its longest title is the one DiscDock reads and offers.

**Finish with what's rescued** stops reading and keeps the movie exactly as far as the disc gave it, however much is missing; the gaps are recorded like any other damage. DiscDock normally refuses a copy that is much shorter than the movie should be, but not when you asked for it.

Where two seconds or more could not be read, the movie freezes. After a best-effort recovery DiscDock finishes the movie as it was read and asks, on the dashboard and in the Library, whether to keep it like that or add loading screens. A loading screen says the disc is scratched, shows when the movie continues (for example "Skip to 00:30:58") and counts down, and a chapter mark lets you skip it. Only a few seconds around each damaged moment are encoded again, in the movie's own video format (MPEG-2 for DVDs, H.264 for most Blu-rays); the rest of the video, the audio and the subtitles are copied unchanged. Movies in other video formats, or where the joined pieces would not play cleanly, are encoded as a whole, which takes much longer. The movie as it was read from the disc is kept in the same folder, named "… - without loading screens". In Settings you can choose to always add loading screens, or never ask. The Library lists every damaged moment of each movie.

**AI repair** is optional. It replaces short damaged moments, up to four seconds each, with frames generated by OpenAI's image models. DiscDock first analyses the movie for free and shows what it would replace and the most it can cost. Nothing is sent to OpenAI until you approve. You need your own OpenAI API key. Generated frames are a best guess at the missing picture, not the original footage. The movie without AI frames is kept next to the repaired one.

**Replace broken parts** works on a movie that is already in your library. The check right after a rip only sees the moments the disc never gave at all, so a movie that breaks up where the disc was scratched can look undamaged. *Replace broken parts* in the Library decodes the whole movie to find every broken moment, lists them, and works out for free what replacing them with AI would cost. Nothing is changed and nothing is sent anywhere. You then choose: keep the movie as it is, cover the moments with loading screens, or replace the frames with AI for at most the price shown. When the damage is too long for AI — it can only draw over a gap of up to four seconds — the Library says so instead of leaving the option out without a word. A repaired movie goes back into the same library folder, with the untouched version kept next to it.

MakeMKV decrypts some Blu-rays only with information it reads from the drive, so it cannot read them from a rescued disc image alone. DiscDock recognises when MakeMKV gets stuck on such an image. It lets MakeMKV start a backup of the disc just long enough to save that information, and then extracts the movie from the rescued files. Keep the disc in the drive until the movie is extracted.

In a long stretch of dense damage, MakeMKV can still stop partway through the movie. DiscDock then lets MakeMKV's library for Blu-ray players (libmmbd, installed with MakeMKV) decrypt the rescued movie, and copies it with FFmpeg, which skips what it cannot use. The decrypting still happens in MakeMKV. Spots the rescue could not read become short gaps that you can cover with loading screens.

**One button at a time.** A disc that needs attention shows the one action worth taking — *Repair* for a damaged disc, *Replace broken parts* for a finished movie — with anything else behind the ⋯ menu next to it.

## Requirements

- Windows 10 version 1809 or later, or Windows 11, 64-bit
- A DVD or Blu-ray drive, internal or USB
- [MakeMKV](https://www.makemkv.com/) for DVDs and Blu-rays
- [FFmpeg](https://ffmpeg.org/) for checking rips, damaged-disc repair and loading screens
- Optional: [HandBrake CLI](https://handbrake.fr/) for smaller files, [cyanrip](https://github.com/cyanreg/cyanrip) for audio CDs and [VLC](https://www.videolan.org/) for previews
- Optional: an [OMDb API key](https://www.omdbapi.com/apikey.aspx) for metadata and an [OpenAI API key](https://platform.openai.com/) for AI repair

Setup can install FFmpeg, HandBrake CLI and cyanrip for you with winget. Install MakeMKV and VLC yourself.

## Install

1. Download `DiscDock-Setup-<version>.exe` from the Releases page and run it. It installs for your Windows account and does not need administrator rights.
2. Install MakeMKV and open it once to accept its license.
3. Open DiscDock from the Start menu. The dashboard opens in your browser at `http://127.0.0.1:8199`. Check **Settings** for tool locations and optional API keys.

DiscDock starts when you sign in, unless you turn that off during setup, and runs in the background. Windows SmartScreen may warn about the installer because it is not code-signed.

To update, run the new Setup. It asks DiscDock to close, waits until it has stopped, replaces its files, starts DiscDock again and checks that the new version answers. Setup does not update while DiscDock is working on a disc.

Ripped media, settings, logs and the job database are stored in the `DiscDock` folder in your user profile. Uninstalling keeps that folder.

## Build from source

You need Python 3.12, Node.js 22 or newer and, for the installer, [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r service\requirements-dev.txt
npm ci
.\scripts\build-release.ps1     # runs the checks and builds release\DiscDock
.\scripts\build-installer.ps1   # builds release\installer\DiscDock-Setup-<version>.exe
```

`scripts\install.ps1` installs a local build without the installer. With `-ArmConfigPath` it imports the API keys from an existing Automatic Ripping Machine `arm.yaml`.

During development, start the service from the `service` folder with `..\.venv\Scripts\python.exe -m discdock` and the dashboard with `npm run dev`.

Checks:

```powershell
.\.venv\Scripts\python.exe -m ruff check service
cd service; ..\.venv\Scripts\python.exe -m pytest -q; cd ..
npm run typecheck
npm run lint
```

## Privacy and security

- The dashboard and API listen only on `127.0.0.1`. Other websites open in your browser cannot read from DiscDock or send it commands.
- API keys and notification URLs are encrypted with Windows DPAPI for your account and are kept out of the logs.
- DiscDock contacts OMDb only for metadata lookups, OpenAI only after you approve an AI repair, and your notification services only to send notifications.
- Rips go to a temporary folder first and are checked before they are moved into the library.
- DiscDock does not use Windows AutoPlay or AutoRun. It checks the drives itself, so AutoPlay can stay turned off.

## Legal

Only rip discs you own, for your own use, and follow the laws where you live. In some countries it is illegal to bypass copy protection. DiscDock does not include MakeMKV, decryption keys or copyrighted media.

## License

MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
