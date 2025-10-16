# driver_ws_tester.py
import argparse, asyncio, json, sys, time
import websockets

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True, help="ex: 127.0.0.1:8000")
    p.add_argument("--driver-id", required=True, type=int)
    p.add_argument("--area", default="city-default")
    p.add_argument("--category", choices=["eco","clim","vip"], default="eco")
    p.add_argument("--auto-accept", dest="auto_accept", action="store_true",
                   help="Accepte automatiquement les courses reçues")
    p.add_argument("--retries", type=int, default=5)
    args = p.parse_args()

    url = f"ws://{args.host}/ws/rides/driver/{args.driver_id}/?area={args.area}&category={args.category}"
    print(f"[TESTER] Connecting to: {url}", flush=True)

    attempt = 0
    while True:
        try:
            async with websockets.connect(url) as ws:
                print("[TESTER] WS OPENED", flush=True)

                # Boucle de réception
                last_ping = time.time()
                while True:
                    # petit ping régulier pour garder la connexion vivante
                    if time.time() - last_ping > 25:
                        try:
                            await ws.send(json.dumps({"type": "ping", "t": time.time()}))
                            print("[TESTER] → ping", flush=True)
                        except Exception as e:
                            print("[TESTER] ping send error:", e, flush=True)
                        last_ping = time.time()

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        print("[TESTER] recv timeout, continue…", flush=True)
                        continue

                    print("[TESTER] ← message:", msg, flush=True)
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue

                    if data.get("type") == "ride.requested":
                        ride = data.get("ride")
                        print(f"[TESTER] RIDE REQUESTED → {ride}", flush=True)
                        if args.auto_accept and ride and ride.get("id"):
                            try:
                                await ws.send(json.dumps({
                                    "action": "accept",
                                    "ride_id": ride["id"]
                                }))
                                print("[TESTER] → sent accept", flush=True)
                            except Exception as e:
                                print("[TESTER] accept send error:", e, flush=True)

        except KeyboardInterrupt:
            print("\n[TESTER] Stopped by user.", flush=True)
            sys.exit(0)
        except Exception as e:
            attempt += 1
            print(f"[TESTER] WS error: {repr(e)} (attempt {attempt})", flush=True)
            if attempt >= args.retries:
                print("[TESTER] Max retries reached. Exiting.", flush=True)
                sys.exit(1)
            await asyncio.sleep(2)  # retry

if __name__ == "__main__":
    asyncio.run(main())