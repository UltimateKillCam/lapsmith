import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 5607))
s.settimeout(15)
print("Listening on UDP 5607 — go drive for ~10s...")
got = 0
try:
    while got < 20:
        data, addr = s.recvfrom(4096)
        got += 1
        print(f"packet {got}: {len(data)} bytes from {addr[0]}")
except socket.timeout:
    print("No packets received in 15s.")
print(f"Total: {got}")
