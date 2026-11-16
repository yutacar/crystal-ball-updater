# Crystal Ball Updater

Crystal Memory Video Ball（型番: crystal 701）向けのLinux常駐ツールです。USB接続で動画をコピーし、
切断すると再生する、このディスプレイの動画を定期的に自動更新します。
時計動画の生成から、指定UUID・USBポートへの定期転送、再生用の接続切断までを提供します。
標準の時計生成器を同梱しているので、別の生成プログラムは不要です。ニュース・天気などの
独自動画へ差し替える場合は、外部生成コマンドを設定できます。

> **English summary** — A small Linux daemon for the Crystal Memory Video Ball (model: crystal 701),
> a USB "copy a video, unplug to play" display.
> Every 30 minutes it renders a clock video that starts at a future minute, copies it to the
> display (identified by filesystem UUID *and* USB port), verifies the copy by SHA-256, unmounts,
> and switches the USB port off exactly at that minute with `uhubctl` so playback starts in sync.
> The bundled clock generator can be replaced by any command that writes an MP4.

## 動作の流れ

1回の更新（サイクル）は次の順で進みます。どこかで失敗すると、古い動画を残したまま非ゼロ終了します。

1. 再生開始時刻を決める（現在の分を切り捨て + `lead_minutes` + 1分）。
2. その時刻から始まる動画を生成し、FFprobeで動画ストリームと長さを確認する。
3. USBポートをONにし、UUIDと接続ポートの両方が一致する機器を待つ。
4. マウントし、機器上の一時ファイルへコピーしてSHA256を照合してから、同名ファイルを置換する。
5. アンマウントし、マウントが残っていないことを確認する。
6. 開始時刻ちょうどにUSBポートをOFFにする（ディスプレイが再生を始める）。
7. `last_success.json` を書き、次回（`interval_minutes` 後）の `lead_minutes` 前まで待つ。

## 機能

- 標準30分間隔。生成を先行して、予定した分境界にUSB接続を切断。
- コピー後のSHA256照合、同一ファイルの置換、アンマウント後の切断。
- 無効な動画、短すぎる動画、期限超過、機器未検出時は非ゼロ終了。
- ロックで多重実行を防ぎ、状態ディレクトリに成功・失敗・次回予定を保存。
- 時計生成を標準提供。任意の外部生成コマンドもJSONのargv配列で指定できます。
- Raspberry Pi 5の共通USB給電へ対応（明示オプション、既定OFF）。

## 要件

- Linux（sysfs・`lsblk`・`mount` を使います。macOS / Windowsでは時計動画の生成のみ可能）
- Python 3.11以上
- [Pillow](https://python-pillow.org/)、FFmpeg（libx264・ffprobe）、[uhubctl](https://github.com/mvp/uhubctl)、TrueTypeフォント
- ポート単位で給電を切り替えられるUSBハブ（uhubctl対応機種）
- Crystal Memory Video Ball（crystal 701）。FATまたはexFATでフォーマットされていること（マウント時に `uid=` / `gid=` を指定します）

標準時計はDejaVu Sans Monoを優先して自動探索します。独自生成コマンドの依存は別途必要です。

## クイックスタート

```bash
# Raspberry Pi OS / Debian系の例
sudo apt install python3-pil ffmpeg uhubctl fonts-dejavu-core
python3 -m unittest discover -v
cp config.example.json config.json
# config.jsonを実機に合わせて編集
python3 crystal_ball_updater.py --config config.json
```

`uuid` は `lsblk -o NAME,UUID,MOUNTPOINTS`、`hub` / `port` は `uhubctl` とsysfsで照合します。
デバイス名 `/dev/sda` を固定しません。`filename` は本体で選択する動画と同名にしてください。
ほかのサンプル動画は削除しません。最初の再生ファイル選択と再生遅延は実機で確認してください。

まず `"cycles": 1` で1回だけ実行し、画面を目視で確認してから常駐（`"cycles": 0`）へ切り替えることを勧めます。

### 権限

`uhubctl`・`mount`・`umount` はrootで実行する必要があります。rootで起動していない場合、本ツールは
これらを `sudo -n`（非対話）で呼び出すため、パスワードなしで実行できる設定が必要です。

```
# /etc/sudoers.d/crystal-ball-updater （visudo -f で編集。パスは command -v で確認）
youruser ALL=(root) NOPASSWD: /usr/sbin/uhubctl, /usr/bin/mount, /usr/bin/umount
```

`mount` を無条件に許可すると、そのユーザーは実質的にroot相当の操作ができます。専用ユーザーで
動かす、あるいはrootのシステムサービスとして動かすなど、環境に合った方法を選んでください。

## 設定

設定はJSONファイル（`--config PATH`。ほかの引数とは併用不可）か、同名のコマンドライン引数
（`--interval-minutes 30` など。`python3 crystal_ball_updater.py --help` 参照）で指定します。
未知のキーはエラーになります。

| キー | 既定値 | 説明 |
| --- | --- | --- |
| `uuid` | （必須） | ディスプレイのファイルシステムUUID。 |
| `hub` | （必須） | uhubctlのハブ位置。例 `"1"`、`"1-1"`。 |
| `port` | （必須） | ハブ上のポート番号。 |
| `filename` | `00_clock_auto.mp4` | 置換する動画ファイル名。英数字・`_`・`-` と `.mp4` のみ。 |
| `interval_minutes` | `30` | 更新間隔（分）。 |
| `duration_minutes` | `35` | 生成する動画の長さ（分）。`interval_minutes + lead_minutes` 以上。 |
| `lead_minutes` | `3` | 生成・接続・転送に確保する先行時間（分）。生成コマンドのタイムアウトにもなります。 |
| `device_timeout` | `20` | 機器の認識を待つ秒数（0より大きく120以下）。 |
| `cycles` | `1` | 実行回数。`0` で常駐。 |
| `state_dir` | `~/.local/state/crystal-ball-updater` | ロック・状態ファイル・一時ファイルの置き場。 |
| `producer_command` | 同梱の時計生成器 | 外部生成コマンドのargv配列（後述）。 |
| `pi5_shared_power` | `false` | Raspberry Pi 5の全ポート給電サイクルを許可（後述）。 |

### 状態ディレクトリ

| ファイル | 内容 |
| --- | --- |
| `last_success.json` | 直近に成功した転送・切断の開始時刻と次回予定（`next_start`）。 |
| `last_failure.json` | 直近の失敗時刻とエラーメッセージ。 |
| `refresh.lock` | 多重実行防止のロック。 |
| `mount/` | 自動マウントされていない場合のマウントポイント。 |
| `crystal-clock-*/` | サイクルごとの一時作業ディレクトリ（終了時に削除）。 |

`last_success.json` は転送・接続切断の完了を示し、画面を見たことは意味しません
（`visual_playback_verified` は常に `false`）。`next_start` は次の予定であり、
将来の更新成功を保証する値ではありません。

## 標準の時計生成と表示

`config.example.json` のUUID・hub・port・state_dirを実環境に合わせれば、そのまま時計として動きます。
`producer_command` を省略した設定は、同梱の `generate_clock_video.py` を呼び出します。
黒背景・480×480・H.264・15fps・無音で、中央に時分、下に日付と曜日を流します。
予定した開始時刻から1分ずつ進む動画を作成し、転送後にその分境界でUSB接続を切断します。
PC単体での動画生成も可能です。

```bash
python3 generate_clock_video.py --start-time 2026-11-16T12:00:00+09:00 \
  --duration-minutes 35 --output clock.mp4
# 外観: --color '#00FFE0' --speed 12 --font /path/to/font.ttf
```

再生開始の遅延・先頭から再生されるか・選択ファイルは実機依存です。
転送と切断の成功は、実画面の正確な同期を保証するものではありません。

## 独自の動画への拡張

`producer_command` を追加すると標準時計を置き換えます。例えば設定JSONへ次を追加します。

```json
"producer_command": ["/usr/bin/python3", "/path/to/generator.py",
  "--start-time", "{start}", "--duration-minutes", "{duration}", "--output", "{output}"]
```

`producer_command` はJSON文字列配列です。シェルを介さず、次の値を置き換えて実行します。

- `{start}`: 再生開始日時、ローカルタイムゾーンのISO 8601形式。
- `{duration}`: 動画の分数。標準35分。
- `{output}`: 完成したMP4の保存先。このプレースホルダーは必須。

生成コマンドは指定した日時から進む動画を作り、完了時に0を返してください。
アップデーターがFFprobeで動画ストリームと長さを確認してからUSBへ転送します。
`lead_minutes`（標準3分）は、生成・機器接続・転送が完了する十分な時間に設定します。
35分動画を30分ごとに更新するため、通常は末尾まで再生せず次の動画へ切り替わります。
期間の基準は最初の開始時刻であり、生成完了ごとに30分を加算する方式ではありません。

`cycles: 0` で常駐、`cycles: 1` で1回だけ実行。ネットワーク取得は生成側で行います。
機器未認識時は期限内で待ち、失敗を記録して終了します。systemdが再起動できます。
失敗すると表示情報が古くなる可能性があるので、`last_failure.json` とjournalを確認してください。

## Raspberry Pi 5

Pi 5はポート別給電に対応すると報告しますが、物理4端子のVBUSは共通です。
対象1ポートのOFF表示だけでは給電が切れません。
[uhubctl公式のPi 5説明](https://github.com/mvp/uhubctl#raspberry-pi-5)を参照してください。

`pi5_shared_power: true` を明示すると、通常接続で認識できない場合に全6論理ポートを
10秒OFFにして元の状態へ戻します。Pi 5・想定ハブ構成・他USB機器の不在・未マウントを
確認し、条件が満たされなければ停止します。USB外付けディスク、キーボード等を併用する
環境ではこの方式を使わず、対象だけの給電を切れる対応ハブを検討してください。
機器の抜き差しを完全に再現する保証や、端子電圧の測定機能はありません。

「他USB機器の不在」は、接続中のUSB機器が対象ポートのディスプレイ（USB ID `10d6:1101`）だけで
あることで判定します。異なるIDのディスプレイで使う場合は `crystal_ball_updater.py` の
`DISPLAY_USB_ID` を書き換えてください（`lsusb` で確認できます）。

## systemdで常駐

付属unitはユーザーサービスの例です。パスと権限を実環境に合わせて確認してください。

```bash
mkdir -p ~/.local/share/crystal-ball-updater ~/.config/crystal-ball-updater ~/.config/systemd/user
cp crystal_ball_updater.py generate_clock_video.py ~/.local/share/crystal-ball-updater/
cp config.json ~/.config/crystal-ball-updater/
cp crystal-ball-updater.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crystal-ball-updater.service
journalctl --user -u crystal-ball-updater.service -f
# 停止: systemctl --user disable --now crystal-ball-updater.service
```

常駐させる設定では `"cycles": 0` にしてください。失敗時は60秒後にsystemdが再起動します。
ログイン前から動かす場合は管理者がlinger（`loginctl enable-linger`）または適切なシステムサービスを
設定してください。画面の表示は本体の電池・再生ファイル選択・再生開始遅延にも依存します。

## トラブルシューティング

| メッセージ | 原因と対処 |
| --- | --- |
| `USB device UUID=… not detected` | 機器が認識されていません。ケーブル、`uuid`、`device_timeout`、ハブの給電切替対応を確認。 |
| `UUID … is not on expected USB port` | 同じUUIDの機器が別のポートにあります。`hub` / `port` を確認。 |
| `unsafe destination or stale upload` | 機器上に前回中断したコピーの `.clock-upload.tmp` が残っています。内容を確認して手動で削除。 |
| `transfer deadline too close` | 生成や認識に時間がかかり、開始時刻まで15秒を切りました。`lead_minutes` を増やす。 |
| `playback deadline missed` | コピーが開始時刻を過ぎました。動画は置換済みですが切断していません。`lead_minutes` を増やす。 |
| `sudo: a password is required` | 非対話sudoが未設定です。「権限」を参照。 |
| `another instance holds …refresh.lock` | 同じ `state_dir` で別のプロセスが動作中です。 |

コピーは新旧2つのファイルが同時に載る空き容量を必要とします。

## 制限

- 実画面を確認する手段はありません。成功の記録は「転送して切断した」ことだけを意味します。
- 時計はUTCオフセット固定で進めるため、夏時間の切替をまたぐ動画は次の更新まで1時間ずれます。
- 対応ファイルシステムはFAT / exFATを想定しています。

## 開発

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -v
```

テストはハードウェア境界（uhubctl・mount・FFmpeg）を模擬するため、Linux以外やFFmpegなしでも
実行できます（フォントは必要です）。IssueやPull Requestを歓迎します。実機のUUID、生成済み動画、
ログ、個人の設定ファイルは含めないでください。

## License

[MIT](LICENSE)
