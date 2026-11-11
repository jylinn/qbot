# 雲端 24 小時部署（Oracle Cloud 免費主機）

> 畫面和選項名稱可能隨 Oracle 改版而不同，以下是實際走過一遍的流程。

## 1. 註冊與選區域

- 申請 Oracle Cloud Free Tier。帳號永久有效；「30 天」指的是附贈的試用點數，Always Free 資源沒有期限。
- **主區域（Home Region）註冊後不能改**。建議選日本（東京或大阪）：幣安伺服器在東京，延遲最低。美國、部分歐洲與新加坡的雲端 IP 有被幣安擋的回報（HTTP 451）。

## 2. 開主機

- Compute → Instances → Create instance
- Image：Ubuntu 22.04
- Shape：`VM.Standard.A1.Flex`（1 OCPU／6 GB）；沒有空位（Out of capacity）就改 `VM.Standard.E2.1.Micro`（1 GB 記憶體，機器人實際只用約 150 MB）
- SSH 金鑰：建議在自己電腦用 `ssh-keygen -t ed25519 -f ~/.ssh/qbot` 產生，只把 `.pub` 公鑰貼給 Oracle，私鑰從頭到尾不離開你的電腦
- 建好後到 VNIC → IP administration，把公開 IP 改成 **Reserved（保留）IP**，之後幣安金鑰要綁這個 IP

## 3. 安裝

```bash
ssh -i ~/.ssh/qbot ubuntu@<你的IP>
# 小主機加 2 GB 虛擬記憶體
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo timedatectl set-timezone UTC
sudo apt update && sudo apt install -y python3-venv
python3 -m venv ~/qbot-venv
```

在自己電腦上傳程式並安裝套件：

```bash
scp -i ~/.ssh/qbot -r livebot ubuntu@<你的IP>:~/
ssh -i ~/.ssh/qbot ubuntu@<你的IP> "~/qbot-venv/bin/pip install -r ~/livebot/requirements.txt"
```

## 4. 試跑與設成服務

```bash
cp ~/livebot/.env.example ~/livebot/.env && chmod 600 ~/livebot/.env
~/qbot-venv/bin/python -m livebot.bot once        # 看到「狀態：總權益…」代表連得到幣安
sudo cp deploy/qbot.service /etc/systemd/system/   # 或從 repo 上傳
sudo systemctl daemon-reload && sudo systemctl enable --now qbot
```

程式當掉 30 秒後自動重啟，主機重開機自動啟動。

| 想做的事 | 指令 |
|---|---|
| 看狀態 | `~/qbot-venv/bin/python -m livebot.bot status` |
| 看即時紀錄 | `tail -f ~/livebot/state/bot.log` |
| 停止／重啟 | `sudo systemctl stop qbot`／`restart qbot` |
| 暫停開新倉 | `touch ~/livebot/state/HALT`（刪掉就恢復） |
| 全部平倉並暫停 | `~/qbot-venv/bin/python -m livebot.bot flatten --yes` |

## 5. 免費主機被回收時自動開回來

Oracle 會把連續 7 天 CPU、網路都很閒的 Always Free 主機停機。除了升級成 Pay As You Go（只用免費資源仍不收費），也可以用內建的 **Resource Scheduler** 定時送開機指令：

1. Resource Scheduler → Create a schedule：Action = Start，選你的主機，間隔例如每 4 小時（建議排在策略判斷時點前 10 分鐘）。
2. Identity → Policies 加一條讓排程有權限開機：

   ```
   Allow any-user to manage instances in tenancy where all {request.principal.type='resourceschedule'}
   ```

主機開著時這個指令沒有作用；被停機的話會在下一次排程時開回來，機器人跟著自動啟動並補算錯過的 K 線。

## 6. 改成實盤

1. 紙上交易先跑幾週，確認沒有錯誤、成本和預期一致。
2. 自己在幣安建立 API 金鑰：只開讀取、合約（放貸另需現貨及萬向劃轉），**不要開提現**，IP 限制填主機的保留 IP。
3. 自己連進主機 `nano ~/livebot/.env` 填入 `MODE=live` 與金鑰。
4. 停服務、刪掉紙上交易的 `state/`，執行 `python -m livebot.bot check`（開了提現權限會拒絕啟動）與 `setup`，再啟動服務。
