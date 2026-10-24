# NENE Edge TTS Relay

This is the only thing you need to run on your Windows PC/Laptop to make your NENE Smart Desk Clock speak.

## Requirements
- Windows PC/Laptop connected to the **SAME Wi-Fi** as the NENE clock
- Internet connection (needed to reach Microsoft's Edge TTS)
- Python will be installed automatically by `start_relay.bat` if missing

## Setup Instructions

### 1) Download
Download or extract this entire folder to your PC (e.g. `Desktop\nene-edge-relay\`)

### 2) Run the relay
1. Open the folder
2. Double-click `start_relay.bat`
3. The first time may take a minute to install dependencies
4. Wait until you see:
   ```
   mDNS advertised as nene-relay.local:8123
   NENE Edge TTS relay ready: http://0.0.0.0:8123  (voice=fil-PH-BlessicaNeural, rate=+0%, pitch=+0Hz, needs internet to Microsoft's Edge TTS)
   ```
5. **Leave this black window open. DO NOT CLOSE IT.**

### 3) Connect the Clock to Wi-Fi
1. Power on your NENE Smart Desk Clock
2. If it doesn't auto-connect, connect your phone/laptop to `NENE-Setup` with password: `nene1234`
3. Go to `http://192.168.4.1` and select/connect to your home Wi-Fi
4. The clock will connect and return to the clock face

### 4) Use it
1. Make sure your PC is on the **SAME Wi-Fi** as the clock
2. Keep the relay window running
3. Tap, double-tap, or long-press the copper pad and it will speak automatically

## Troubleshooting
- **No voice** – Make sure the relay window is open and your PC/laptop is on the same Wi-Fi as the clock
- **Voice stops after sleep** – Windows put your PC to sleep. Wake it up or disable sleep while using it
- **Firewall prompt** – If Windows Firewall asks, allow `python.exe` on **Private** network
- **Still no voice** – Close the relay window and run `start_relay.bat` again

**Tip:** You don't need to find any IP address. The clock finds this relay automatically over mDNS as `nene-relay.local`
