.PHONY: setup venv run test test-real-iphone

# make -f makefile          -> Systemabhängigkeiten installieren (Default)
# make -f makefile venv     -> venv anlegen, Python-Pakete installieren
# make -f makefile run      -> Server starten
# make -f makefile test     -> Reproduktions-Test mit simulierter iPhone-Quelle
# make -f makefile test-real-iphone -> derselbe Test gegen ein echtes iPhone

setup:
	sudo apt update
	sudo apt install -y \
		python3 \
		python3-gi \
		python3-gst-1.0 \
		gir1.2-gstreamer-1.0 \
		gstreamer1.0-tools \
		gstreamer1.0-plugins-base \
		gstreamer1.0-plugins-good \
		gstreamer1.0-plugins-bad \
		gstreamer1.0-plugins-ugly \
		gstreamer1.0-libav
	gst-launch-1.0 --version
	python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())"

venv:
	python3 -m venv --system-site-packages .venv
	.venv/bin/pip install fastapi pydantic uvicorn pytest httpx

run:
	.venv/bin/python3 server.py

test:
	.venv/bin/pytest -s test_switch_repro.py

test-real-iphone:
	LICHT_TEST_REAL_IPHONE=1 LICHT_TEST_SRT_PORT=6001 LICHT_TEST_SINK=kmssink LICHT_TEST_CYCLES=10 \
		.venv/bin/pytest -s test_switch_repro.py
