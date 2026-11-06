from rgpu.lan_audio import (AudioMonitor, BLOCK_BYTES, MAX_LATENCY_BYTES, MAX_PCM_BYTES,
                            PREBUFFER_BYTES)


class FakeCard:
    """Plays one queued block whenever play() is called (the sound card's clock)."""
    def __init__(self):
        self.queue, self.played = [], []

    def write(self, pcm):
        self.queue.append(pcm)

    def queued(self):
        return len(self.queue)

    def play(self):
        if self.queue:
            self.played.append(self.queue.pop(0))

    def reset(self):
        self.queue.clear()

    def close(self):
        pass


def chunk(index):
    return bytes([index % 251, 0]) * (MAX_PCM_BYTES // 2)


def make():
    card = FakeCard()
    monitor = AudioMonitor(lambda e: None, speaker_factory=lambda: card)
    monitor.start()
    return monitor, card


def test_waits_for_prebuffer_then_plays_in_order_without_gaps():
    monitor, card = make()
    for i in range(PREBUFFER_BYTES // MAX_PCM_BYTES - 1):
        monitor.receive(chunk(i))
    assert card.queued() == 0                        # still collecting
    sent = PREBUFFER_BYTES // MAX_PCM_BYTES - 1
    for i in range(sent, 200):                       # steady 20 ms packets, card plays 40 ms per 2 packets
        monitor.receive(chunk(i))
        if i % 2:
            card.play()
    stream = b"".join(chunk(i) for i in range(200))
    played = b"".join(card.played)
    assert stream.startswith(played) and len(played) > 150 * MAX_PCM_BYTES
    assert monitor.underruns == 0 and monitor.skipped_bytes == 0


def test_burst_after_wifi_stall_is_kept_not_dropped():
    monitor, card = make()
    for i in range(10):
        monitor.receive(chunk(i))
    card.play(); card.play()
    # 100 ms stall then the 5 delayed packets arrive at once: nothing may be lost.
    for i in range(10, 15):
        monitor.receive(chunk(i))
    while card.queued():
        card.play()
    assert b"".join(card.played) == b"".join(chunk(i) for i in range(len(card.played) * 2))
    assert monitor.skipped_bytes == 0


def test_long_backlog_is_trimmed_to_bound_latency():
    monitor, card = make()
    for i in range(100):                             # 2 s arrive while the card is stuck
        monitor.receive(chunk(i))
    assert card.queued() * BLOCK_BYTES + len(monitor.buffer) <= MAX_LATENCY_BYTES + BLOCK_BYTES
    assert monitor.skipped_bytes > 0


def test_underrun_refills_before_resuming():
    monitor, card = make()
    for i in range(6):
        monitor.receive(chunk(i))
    while card.queued():
        card.play()                                  # card ran dry (network gap)
    monitor.receive(chunk(6))
    assert monitor.underruns == 1 and card.queued() == 0   # waits for 120 ms again instead of stuttering
    for i in range(7, 12):
        monitor.receive(chunk(i))
    assert card.queued() == PREBUFFER_BYTES // BLOCK_BYTES
