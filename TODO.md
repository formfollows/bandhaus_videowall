- buttons
- h265
- video switcher
- test mode, screenshot endpoint (png is now written periodically, needs HTTP endpoint)


gst-launch-1.0 -v \
    srtsrc uri="srt://0.0.0.0:6001" mode=listener latency=200 \
    ! tsdemux name=demux \
    demux. ! queue max-size-bytes=0 max-size-buffers=1 max-size-time=0 \
    ! h265parse \
    ! v4l2slh265dec \
    ! video/x-raw,format=NV12 \
    ! glimagesink sync=false


gst-launch-1.0 -v -m \
  srtsrc uri="srt://:6001?mode=listener&latency=120" \
  ! tsdemux name=d \
  d. ! queue \
       max-size-buffers=3 \
       max-size-bytes=0 \
       max-size-time=0 \
       leaky=downstream \
  ! h265parse \
  ! v4l2slh265dec \
  ! kmssink sync=false enable-last-sample=false



  srt://192.168.178.108:6001?mode=caller&latency=120


  Wir haben trotzdem noch ein offenes Thema:
v4l2slh265dec -> kmssink mit DMA-BUF liefert auf deinem System ein kaputtes Bild.
v4l2slh265dec -> videoconvert -> kmssink funktioniert.


gst-launch-1.0 -v \
  srtsrc uri="srt://:6001?mode=listener&latency=40" \
    keep-listening=true \
  ! tsdemux name=d latency=0 \
  d.video_0_0100 \
  ! queue \
      max-size-buffers=30 \
      max-size-bytes=0 \
      max-size-time=0 \
      leaky=no \
  ! h265parse \
  ! v4l2slh265dec \
  ! videoconvert \
  ! videoscale \
  ! video/x-raw,format=I420,width=800,height=450 \
  ! queue \
      max-size-buffers=1 \
      max-size-bytes=0 \
      max-size-time=0 \
      leaky=downstream \
  ! kmssink \
      sync=false \
      async=false \
      enable-last-sample=false


    gst-launch-1.0   srtsrc uri="srt://:6001?mode=listener&latency=40" keep-listening=true   ! tsdemux name=d latency=0   d.video_0_0100   ! h265parse   ! v4l2slh265dec   ! videoconvert   ! videoscale   ! video/x-raw,format=I420,width=800,height=450   ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream   ! kmssink sync=false