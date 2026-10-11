from app.audio.icecast_audio_sink import IcecastAudioSink


def test_output_recovery_preserves_queued_and_inflight_pcm():
    sink = IcecastAudioSink("ffmpeg", lambda *_args, **_kwargs: None)
    sink._pcm_queue.put_nowait(b"queued-programme-audio")
    sink._pcm_dispatch_queue.put_nowait(b"queued-dispatch-audio")
    sink._writer_pending_chunk = b"inflight-writer-audio"
    sink._pcm_dispatch_pending_chunk = b"inflight-dispatch-audio"

    sink.stop(preserve_pcm=True)

    assert sink._pcm_queue.get_nowait() == b"queued-programme-audio"
    assert sink._pcm_dispatch_queue.get_nowait() == b"queued-dispatch-audio"
    assert sink._writer_pending_chunk == b"inflight-writer-audio"
    assert sink._pcm_dispatch_pending_chunk == b"inflight-dispatch-audio"

    # Ordinary shutdown remains responsible for clearing media buffers.
    sink.stop()
    assert sink._pcm_queue.empty()
    assert sink._pcm_dispatch_queue.empty()
    assert sink._writer_pending_chunk is None
    assert sink._pcm_dispatch_pending_chunk is None
