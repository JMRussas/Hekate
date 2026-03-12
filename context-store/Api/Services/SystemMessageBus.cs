// CodeStoragePoc.Api - System Message Bus
//
// In-memory pub/sub for broadcasting system messages to connected SSE clients.
// Used by POST /api/system-message to push notifications to the UI in real-time.
//
// Depends on: System.Threading.Channels
// Used by:    Api/Program.cs (DI singleton, endpoints)

using System.Collections.Concurrent;
using System.Threading.Channels;

namespace CodeStoragePoc.Api.Services;

public record SystemMessage(string Level, string Text, bool Persist = false, string? ConversationId = null);

public class SystemMessageBus
{
    private readonly ConcurrentDictionary<Guid, Channel<SystemMessage>> _subscribers = new();

    public (Guid Id, ChannelReader<SystemMessage> Reader) Subscribe()
    {
        var id = Guid.NewGuid();
        var channel = Channel.CreateBounded<SystemMessage>(new BoundedChannelOptions(100)
        {
            FullMode = BoundedChannelFullMode.DropOldest
        });
        _subscribers[id] = channel;
        return (id, channel.Reader);
    }

    public void Unsubscribe(Guid id)
    {
        if (_subscribers.TryRemove(id, out var ch))
            ch.Writer.TryComplete();
    }

    public void Publish(SystemMessage message)
    {
        foreach (var ch in _subscribers.Values)
            ch.Writer.TryWrite(message);
    }
}
