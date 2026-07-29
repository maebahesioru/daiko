#!/bin/bash
set -e

# Fix Tor hidden service permissions
chown -R root:root /var/lib/tor/hidden_service 2>/dev/null || true
chmod 700 /var/lib/tor/hidden_service 2>/dev/null || true

# Write torrc
cat > /etc/tor/torrc <<EOF
Log notice stdout
SocksPort 0
HiddenServiceDir /var/lib/tor/hidden_service
HiddenServicePort 80 127.0.0.1:5000
EOF

# Start Tor in background
tor &

# Wait for hidden service hostname to be generated
echo "Waiting for Tor hidden service..."
for i in $(seq 1 30); do
    if [ -f /var/lib/tor/hidden_service/hostname ]; then
        echo "Hidden service ready: $(cat /var/lib/tor/hidden_service/hostname)"
        break
    fi
    sleep 2
done

# Start Flask
cd /app
exec python3 app.py
