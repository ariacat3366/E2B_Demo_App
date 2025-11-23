import json
import os
import subprocess
import threading
import queue

COMMAND = json.loads(r'''$command_json''')
REQUESTS = json.loads(r'''$requests_json''')
ENV_UPDATES = json.loads(r'''$env_updates_json''')


def run():
    env = os.environ.copy()
    env.update(ENV_UPDATES or {})
    process = subprocess.Popen(
        COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    def enqueue_output(out, q):
        for line in iter(out.readline, ""):
            q.put(line)
        out.close()

    q = queue.Queue()
    t = threading.Thread(target=enqueue_output, args=(process.stdout, q), daemon=True)
    t.start()

    results = []
    for req in REQUESTS:
        payload = json.dumps(req) + "\n"
        process.stdin.write(payload)
        process.stdin.flush()
        while True:
            try:
                line = q.get(timeout=30)
            except Exception:
                break
            try:
                data = json.loads(line)
                if data.get("id") == req.get("id"):
                    results.append(data)
                    break
            except json.JSONDecodeError:
                continue

    process.stdin.close()
    process.terminate()
    return results


if __name__ == "__main__":
    print(json.dumps(run()))
