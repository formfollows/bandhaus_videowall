
  srt://192.168.178.108:6001?mode=caller&latency=120




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