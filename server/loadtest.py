"""Load test the BIS server: spawn N simulated drivers each POSTing schedule
updates at a target rate, while sampling the server process's CPU. Reports
achieved throughput, POST latency percentiles, and server CPU.

Usage:  python loadtest.py <port> <n_drivers> <hz> <seconds>
"""
import json, sys, threading, time, urllib.request
import psutil

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
N = int(sys.argv[2]) if len(sys.argv) > 2 else 20
HZ = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
SECS = float(sys.argv[4]) if len(sys.argv) > 4 else 12.0
URL = f"http://127.0.0.1:{PORT}/api/update"

# a realistic up-direction schedule payload (ids from the real 124A route)
stops = json.load(open("../data/route_124A.json", encoding="utf-8"))["stops"]
lat = []            # POST latencies (ms)
errors = [0]
stop = threading.Event()


def driver(idx):
    period = 1.0 / HZ
    i = idx % (len(stops) - 1) + 1        # each driver heads to a different stop
    while not stop.is_set():
        s = stops[i]
        body = json.dumps(dict(id=f"drv-{idx}", nick=f"기사{idx}", line="124", map="Segang Alpha",
            nextIdx=i, nextIdCode=int(s["id"]), nextDist=120.0, prevDist=80.0,
            atStation=0.0, nextName=s["kname"], schedValid=True)).encode()
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            urllib.request.urlopen(req, timeout=5).read()
            lat.append((time.perf_counter() - t0) * 1000)
        except Exception:
            errors[0] += 1
        i = i + 1 if i < len(stops) - 1 else 1     # advance stop occasionally
        time.sleep(period)


def find_server_pid():
    for c in psutil.net_connections(kind="inet"):
        if c.laddr and c.laddr.port == PORT and c.status == psutil.CONN_LISTEN and c.pid:
            return c.pid
    return None


def main():
    pid = find_server_pid()
    proc = psutil.Process(pid) if pid else None
    if proc:
        proc.cpu_percent(None)            # prime the measurement
    threads = [threading.Thread(target=driver, args=(k,), daemon=True) for k in range(N)]
    for t in threads:
        t.start()
    cpu = []
    t_end = time.time() + SECS
    while time.time() < t_end:
        time.sleep(0.5)
        if proc:
            cpu.append(proc.cpu_percent(None))
    stop.set()
    time.sleep(0.3)

    ncpu = psutil.cpu_count()
    lat_sorted = sorted(lat)
    def pct(p):
        return lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * p))] if lat_sorted else 0
    rps = len(lat) / SECS
    cpu_avg = sum(cpu) / len(cpu) if cpu else 0
    cpu_max = max(cpu) if cpu else 0
    print(f"\n=== N={N} drivers @ {HZ}Hz for {SECS:.0f}s (port {PORT}) ===")
    print(f"requests ok      : {len(lat)}   errors: {errors[0]}")
    print(f"throughput       : {rps:.0f} req/s  (target {N*HZ:.0f})")
    print(f"latency  p50/p95 : {pct(.5):.1f} / {pct(.95):.1f} ms   max {pct(1):.1f} ms")
    print(f"server CPU (of 1 core) avg/max : {cpu_avg/ncpu:.0f}% / {cpu_max/ncpu:.0f}%   [{ncpu} cores]")
    print(f"server CPU (psutil raw, 1core=100) avg/max : {cpu_avg:.0f}% / {cpu_max:.0f}%")


if __name__ == "__main__":
    main()
