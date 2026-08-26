import pytest

from adapters.sse import sse_events


async def _iter(chunks):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_lf_framed_events():
    events = [e async for e in sse_events(_iter([b"event: foo\ndata: bar\n\n"]))]
    assert events == [("foo", "bar")]


@pytest.mark.asyncio
async def test_crlf_framed_events():
    events = [e async for e in sse_events(_iter([b"event: foo\r\ndata: bar\r\n\r\n"]))]
    assert events == [("foo", "bar")]


@pytest.mark.asyncio
async def test_cr_only_framed_events():
    events = [e async for e in sse_events(_iter([b"event: foo\rdata: bar\r\r"]))]
    assert events == [("foo", "bar")]


@pytest.mark.asyncio
async def test_multiple_data_lines_joined_with_newline():
    events = [e async for e in sse_events(_iter([b"data: line1\ndata: line2\n\n"]))]
    assert events == [(None, "line1\nline2")]


@pytest.mark.asyncio
async def test_crlf_split_across_chunk_boundary_not_misparsed():
    # "\r\n\r\n" (the blank-line separator) split so a lone trailing "\r" in one
    # chunk and a leading "\n" in the next must not be misread as two separate
    # line endings (which would insert a spurious blank frame).
    chunks = [b"data: bar\r", b"\n\r\n"]
    events = [e async for e in sse_events(_iter(chunks))]
    assert events == [(None, "bar")]


@pytest.mark.asyncio
async def test_trailing_event_without_blank_line_is_flushed():
    events = [e async for e in sse_events(_iter([b"data: last"]))]
    assert events == [(None, "last")]
