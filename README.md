# DeckOps Nightly

<p align="center">
  <img src="assets/images/DeckOps-banner.png" alt="DeckOps - CombatOnDeck" width="460"/>
</p>

<p align="center">
  Bringing the Golden Age of Call of Duty to your Steam Deck and other Linux handhelds and devices, no tinkering required.™️
</p>

---

> **This is the Nightly build of DeckOps. It is unstable and may be broken at any time. Features are experimental and not ready for general use. For the stable release, visit the [main DeckOps repository](https://github.com/GalvarinoDev/DeckOps).**

---

## Supported Games

| Game | Mode | Client | Online | Aim Assist |
|---|---|---|---|---|
| Modern Warfare | SP/MP | [IW3SP-MOD](https://gitea.com/JerryALT/iw3sp_mod) + [CoD4R](https://iw3x.com/) | ✅ | ✅ |
| Modern Warfare 2 | SP/MP | [IW-Pad](https://github.com/GalvarinoDev/IW-Pad) + [iw4x](https://iw4x.io) | ✅ | ✅ |
| Modern Warfare 3 | SP/MP | [IW-Pad](https://github.com/GalvarinoDev/IW-Pad) + [Plutonium](https://plutonium.pw) | ✅ | ✅ |
| World at War | SP/ZM/MP | [Plutonium](https://plutonium.pw) | ✅ | ✅ |
| Black Ops | SP/ZM/MP | [Plutonium](https://plutonium.pw) | ✅ | ✅ |
| Black Ops II | SP/ZM/MP | [T6SP-Mod](https://github.com/Rattpak/T6SP-Mod-Release) + [Plutonium](https://plutonium.pw) | ✅ | ✅ |

DeckOps bundles the Steam Deck Steam Input layout **Call of Duty with controller support** for its managed games. It does not assign a layout to external controllers or other handhelds; configure those in Steam Input yourself. IW-Pad adds controller support and aim assist to MW2 SP and MW3 SP. New MW1 MP setups install CoD4R only; CoD4x is not a setup or update option. Existing CoD4x installations remain recognized for shortcut repair and uninstall; reinstalling MW1 MP (after confirmation) migrates to CoD4R.

IW-Pad and AlterWare do not repair the remote code execution (RCE) vulnerabilities in MW2 SP and MW3 SP. We do not recommend that you play Special Ops in MW2 or MW3. If you play Special Ops, you do so at your own risk.

Supports Steam Deck (LCD and OLED), Steam Machine, Legion Go / Go S / Go 2, ROG Ally / Ally X, MSI Claw 8, and PC. SteamOS, Bazzite, and CachyOS supported.

Call of Duty: Ghosts, Advanced Warfare, and Black Ops III are no longer supported. If you set them up with an earlier version, Full Uninstall still removes their DeckOps files.

---

## Plutonium Offline

With Plutonium Offline, you can play WaW, BO1, BO2, and MW3 without an internet connection.

1. On the setup screen, select **Plutonium Offline**.
2. DeckOps adds the **DeckOps: Plutonium Offline** launcher to your Steam library.
3. To start a game, open the launcher. You can also pick the game's **(Offline)** entry in its Steam launch menu.

In Offline Multiplayer, DeckOps starts a local server with bots. You can also play Campaign and Zombies offline in WaW and BO1, and Zombies offline in BO2.

Plutonium Offline uses a separate instance of Plutonium. It does not interact with your online files.

---

## Before You Install

1. Install your games on Steam first. You do not need to launch them.
2. Have a stable internet connection ready.
3. Plutonium requires a [free account](https://forum.plutonium.pw/register). You must log in one time during the install. After the install, you can play offline on all devices.
4. MW3 requires a depot downgrade. DeckOps downloads [DepotDownloader](https://github.com/SteamRE/DepotDownloader) and uses your Steam account to get the correct game files. You log in through a QR code during the install.

---

## Installation

1. Press the Steam button → **Power** → **Switch to Desktop**.
2. Open a browser and go to this GitHub page.
3. Download the **[DeckOps Nightly file](https://github.com/smeltzzz/DeckOps-Nightly/releases/download/v1/DeckOps-Nightly.desktop)**.
4. Right-click the file → **Properties** → **Permissions** → tick **"Is executable"** → OK.
5. Double-click it.
   - **First time:** DeckOps installs and launches automatically.
   - **Already installed:** A menu appears with Launch and Uninstall options.

DeckOps checks for updates when you launch it. An existing installation can still open without internet. Updates normally download only changed files; if the change list cannot be verified, DeckOps downloads a complete update instead. Your config, logs, and music are preserved. If an update is interrupted, reopen DeckOps to restore the previous version before trying again.

---

## After Installation

Click Continue when installation finishes. DeckOps reopens Steam automatically. First launches take longer while Proton sets things up.

If Steam asks about cloud saves, choose **Keep Local**. If asked about safe mode or hardware changes, choose **No**.

- **MW1.** New MP setups use CoD4R: SP and MP are both on the main entry. Play starts SP; use the Multiplayer button in the main menu for MP. To return to SP, quit and press Play again. Existing CoD4x MP setups keep their separate DeckOps shortcut until migrated to CoD4R. Select the "Player" profile on first SP launch. During install, DeckOps opens the CoD4R launcher. Wait for it to finish downloading and updating. Close the launcher when it is done, then press Continue in DeckOps.
- **MW1 MP.** Launch MW1 MP twice on first install. The first launch fails. Relaunch and it works.
- **WaW.** On OLED and other non-LCD devices, SP/ZM and MP are both on the main entry. The launch menu shows **Campaign & Zombies** and **Multiplayer**; Steam's own WaW entries are hidden from the menu. DeckOps removes the old WaW MP shortcut.
- **BO1 and BO2.** On OLED and other non-LCD devices, the SP entry's launch menu also has the Plutonium Multiplayer (and BO2 Zombies) entries. Run **Repair Shortcuts** to add them. The separate MP and ZM entries stay in your library; hide them in Steam if you don't want them.
- **MW2.** The campaign's Multiplayer button starts the separate MW2 Multiplayer entry. Its launch menu shows only IW4x (Steam's own entry is hidden), so it starts IW4x straight away. Repair Shortcuts applies this to an existing setup.
- **MW3.** The main entry starts the campaign straight away (no menu). Multiplayer (Plutonium) is the separate MW3 Multiplayer entry, which starts Plutonium directly; hide it in Steam if you don't use it. Repair Shortcuts removes the old Multiplayer entry from the main entry's menu.
- **MW3 MP.** If the downgrader fails with a depot access error, add the free MW3 Dedicated Server tool to your Steam account. Paste `steam://install/42750` in a browser or Steam's URL bar, then run the downgrader again.
- **LCD Steam Deck.** The LCD Steam Deck has a broken Proton/Wine stack. It reports the wrong serials to games. In Plutonium, this can cause a false positive HWID ban. DeckOps repairs the Proton stack for the game. DeckOps does not change hardware IDs and does not help you avoid bans. Plutonium games may take a moment to launch during shader cache cleanup. Skip Vulkan shader compilation if prompted. Quit from the in-game menu for a faster exit.

### Updating games

In **My Games**, choose **Update Games** to check and repair the mod-client modes you have already set up, or **Game Options → Update Client** for one game's configured modes. Review the mode list before continuing: other Plutonium modes may be included because they share client files. DeckOps backs up saves first and closes Steam before changing its settings.

Each update also installs the latest GE-Proton. DeckOps then removes GE-Proton versions it installed earlier, keeping the current one and the one before it. It never removes a version that a game, shortcut or the other DeckOps build still uses, or one you installed yourself.

If something fails, the result screen shows what needs attention and offers **Retry Failed Updates**. **Stop After Current Step** waits for the active operation to finish; it does not interrupt a download or installation in the middle of a write. Completed changes are kept. If a save backup is incomplete, you can decline before game files or Steam settings change.

---

## Controller Layout and Deadzones

DeckOps bundles **Call of Duty with controller support** for Steam Deck LCD/OLED controls and assigns it to supported games and DeckOps shortcuts while Steam is closed. Its controls are defined by that layout, which does not explicitly bind gyro; add gyro in Steam Input yourself if you want it. DeckOps no longer offers gyro mode or external-controller presets. For other controllers, pick a layout in Steam Input manually.

Supported game modes receive `gpad_stick_deadzone_min "0.05"` and `gpad_stick_deadzone_max "0.00"` on install and when you open My Games. You can also re-apply the values from Settings. MW1 and BO2 campaign do not use these text-config settings; MW2/MW3 campaigns ignore their text configs.

---

## Settings

| Option | What it does |
|---|---|
| Background Music | Plays music inside DeckOps. Turn it on or off and set the volume. |
| Controller Profiles | **Re-apply Layout** assigns the bundled Steam Deck layout to DeckOps games and shortcuts. Closes Steam; other controller types are untouched. |
| Controller Deadzone | Automatically sets supported game configs to 0.05 minimum and 0.00 maximum. **Re-apply Deadzone** patches existing configs without replacing other settings. |
| Shortcuts | **Repair Shortcuts** re-adds any missing DeckOps shortcuts and repairs the launch menus and launch options of your games. Closes Steam. |
| Save Backup | Copies your saves, stats and Plutonium mods to a folder that survives an uninstall. Steam can stay open. |
| Check My Setup | Checks each game's folder, mod files, Steam launch option, Proton prefix, and that Steam runs it with the GE-Proton DeckOps set up. Also checks DeckOps shortcuts (legacy CoD4x MP, your own copies, Plutonium Offline). It changes nothing; **Fix** runs that game's Update, or **Repair Shortcuts** for the shortcuts. |
| Player Name | Your name in existing CoD4x setups, IW4x, and Plutonium offline. |
| FPS and FOV | **Apply 90 FPS / 90 FOV** writes both into the game configs of MW1 (campaign and multiplayer), MW2 and MW3 multiplayer, WaW, BO1, and BO2 (every mode); nothing else in those files changes. The MW2 and MW3 campaigns (64-bit Steam builds) ignore their config files, so that button does nothing for them. Their FOV is set to 90 automatically every time you start the game (`fovwatch.py`). Their FPS is set by the **MW2/MW3 FPS Limit** button, which limits the frame rate to 90 with DXVK in the launch option (it closes Steam when you change it). |
| Shader Cache (LCD only) | Clears the shader cache of every game you have set up. |
| Updates & Logs | **Check for Updates** looks for a new DeckOps version. **Copy Log** and **View Log** show what DeckOps has done, for bug reports. |
| Full Uninstall | Removes DeckOps from your games and Steam while keeping save backups and unrelated Plutonium data. If Stable is also installed, shared game files and Steam settings stay in place. Set up the same games and Offline Mode in Stable first; uninstall stops rather than leave games pointing at missing configuration. Unverified mods and player data are kept. |
| Reset DeckOps Config | Clears your setup and runs setup again. |
| Links | Support on Discord, and the Stable and Nightly project pages. |

---

## Troubleshooting

https://discord.gg/bkSQeq5Azk

---

## Credits

DeckOps is an installer. This project would not exist without years of foundational work from these teams:

The interface uses [Atkinson Hyperlegible](https://brailleinstitute.org/freefont), licensed under the SIL Open Font License (see `assets/fonts/AtkinsonHyperlegible-OFL.txt`), for readable body text. Russo One remains the display face. The Plutonium Offline launcher is rebuilt with `scripts/build_offline_launcher.ps1`; see `docs/offline-launcher-build.md`.

**[CoD4R](https://iw3x.com/)** - MW1 MP client (Call of Duty 4: Revived) by [k/divity](https://github.com/Divity). Native controller support, aim assist, server browser, and bot support. [Website](https://iw3x.com/) | [Discord](https://discord.com/invite/uWAuFzru34) | 💰 [Ko-fi](https://ko-fi.com/divity)

**[CoD4x](https://cod4x.ovh)** - Legacy MW1 MP client still supported for existing installations, not offered for new setups. [GitHub](https://github.com/callofduty4x)

**[IW3SP-MOD](https://gitea.com/JerryALT/iw3sp_mod)** - MW1 SP client by [JerryALT](https://gitea.com/JerryALT).

**[iw4x](https://iw4x.io)** - MW2 MP client. [GitHub](https://github.com/iw4x)

**[AlterWare](https://github.com/alterware)** - Hosts the MW2 and MW3 campaign client files that DeckOps uses for non-Steam copies.

**[T6SP-Mod](https://github.com/Rattpak/T6SP-Mod-Release)** - Black Ops II SP client by [Rattpak](https://github.com/Rattpak).

**[Plutonium](https://plutonium.pw)** - MW3, WaW, BO1, and BO2 client. 💰 [Donate](https://forum.plutonium.pw/donate)

**[Bot Warfare](https://github.com/ineedbots)** - Bots for Plutonium Offline in WaW, BO1, BO2, and MW3 by [ineedbots](https://github.com/ineedbots).

**[T6ServerConfigs](https://github.com/xerxes-at/T6ServerConfigs)** - BO2 game mode settings for Plutonium Offline by [xerxes-at](https://github.com/xerxes-at).

**[DepotDownloader](https://github.com/SteamRE/DepotDownloader)** - Steam depot downloader by [SteamRE](https://github.com/SteamRE). Used to download the correct game files for MW3.

Official Test Team: [LeFinnaBust](https://github.com/LeFinnaBust) & Special Agent Dale Cooper

---
**[Call of Duty Alt Launcher](https://github.com/framilano/CallofDutyAltLauncher)** - Inspiration for DeckOps.

**[LanLauncher](https://github.com/JugAndDoubleTap/LanLauncher)** - Inspiration for offline LAN mode.

Steam artwork from [SteamGridDB](https://www.steamgriddb.com) - thanks to [Moohoo](https://www.steamgriddb.com/profile/76561198009314736), [jarvis](https://www.steamgriddb.com/profile/76561198103947979), [Ramzej](https://www.steamgriddb.com/profile/76561198122547176), [Over](https://www.steamgriddb.com/profile/76561198049670875), [Uravity-PRO](https://www.steamgriddb.com/profile/76561198167607660), [Maxine](https://www.steamgriddb.com/profile/76561198130550992), [caukyy](https://www.steamgriddb.com/profile/76561198031582867), [Middle](https://www.steamgriddb.com/profile/76561198027273869), [Hevi](https://www.steamgriddb.com/profile/76561198018073166), [europeOS](https://www.steamgriddb.com/profile/76561198038608428), [Empti](https://www.steamgriddb.com/profile/76561198022992095), [grimlokk](https://www.steamgriddb.com/profile/76561199034037601), [Mr.Parks](https://www.steamgriddb.com/profile/76561198018403239), [Dankheili](https://www.steamgriddb.com/profile/76561198040056867), [FaN](https://www.steamgriddb.com/profile/76561198015449572), [adamboulton](https://www.steamgriddb.com/profile/76561198143575007), [ActualCj](https://www.steamgriddb.com/profile/76561198135110632), [KimaRo](https://www.steamgriddb.com/profile/76561197985524535), [Gector(lint)Nathan](https://www.steamgriddb.com/profile/76561198319864298), [increasing](https://www.steamgriddb.com/profile/76561198041593264), [xamon](https://www.steamgriddb.com/profile/76561197979282373), [jakearty](https://www.steamgriddb.com/profile/76561199079444502), [dragnus](https://www.steamgriddb.com/profile/76561198015793633), [Rod](https://www.steamgriddb.com/profile/76561198125292564), and [OnSync](https://www.steamgriddb.com/profile/76561198061208589).

**[Claude](https://claude.ai)** by Anthropic - assisted in development.

---

> DeckOps is not affiliated with Activision, Infinity Ward, Treyarch, or Valve. All trademarks belong to their respective owners. A legitimate copy of each game is required. DeckOps does not provide or distribute game files.

## License

[MIT License](LICENSE)
