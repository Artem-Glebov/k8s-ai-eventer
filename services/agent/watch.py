"""Kubernetes Events watch loop.

No attempt to resume from a saved resourceVersion across reconnects — on any
error we back off and start a fresh list+watch (full relist). A stale replay
of a few already-seen events is harmless for advice-generation, and this is
far simpler/more robust for a solo-maintained loop than resourceVersion
bookkeeping. `timeout_seconds` also forces a periodic reconnect even when
nothing goes wrong, so the relist path is exercised constantly in normal
operation instead of only showing bugs after hours of uptime.
"""

import logging
import threading
import time

import urllib3.exceptions
from kubernetes import client
from kubernetes import watch as k8s_watch
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger("watch")

MAX_BACKOFF = 30
WATCH_TIMEOUT_SECONDS = 300


def _watch_loop(api: "client.CoreV1Api", namespace: str | None, on_event, stop_event: threading.Event):
    w = k8s_watch.Watch()
    backoff = 1
    label = namespace or "*"
    while not stop_event.is_set():
        try:
            if namespace:
                stream = w.stream(api.list_namespaced_event, namespace, timeout_seconds=WATCH_TIMEOUT_SECONDS)
            else:
                stream = w.stream(api.list_event_for_all_namespaces, timeout_seconds=WATCH_TIMEOUT_SECONDS)
            for evt in stream:
                if stop_event.is_set():
                    break
                try:
                    on_event(evt["object"])
                except Exception:
                    logger.exception("on_event handler failed for an event in namespace=%s", label)
                backoff = 1
        except (ApiException, urllib3.exceptions.HTTPError) as e:
            logger.warning("watch stream error (namespace=%s): %s - reconnecting in %ss", label, e, backoff)
        except Exception:
            logger.exception("unexpected watch error (namespace=%s) - reconnecting in %ss", label, backoff)
        finally:
            w.stop()
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def start(api: "client.CoreV1Api", scope: str, namespaces: list[str], on_event, stop_event: threading.Event) -> list[threading.Thread]:
    """Spawns one daemon thread per watched namespace (or one for the whole
    cluster). Returns the thread list; caller decides whether to join them."""
    threads = []
    targets = [None] if scope == "cluster" else namespaces
    for ns in targets:
        t = threading.Thread(target=_watch_loop, args=(api, ns, on_event, stop_event), daemon=True, name=f"watch-{ns or 'cluster'}")
        t.start()
        threads.append(t)
    return threads
