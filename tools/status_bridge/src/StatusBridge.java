import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.atomic.AtomicLong;
import org.wpilib.networktables.NetworkTableEvent;
import org.wpilib.networktables.NetworkTableInstance;
import org.wpilib.networktables.PubSubOption;
import org.wpilib.networktables.StringSubscriber;
import org.wpilib.networktables.TimestampedString;
import org.wpilib.util.WPIUtilJNI;

/** Read-only, exact Alpha7 subscriber. Stdout contains bounded JSON events only. */
public final class StatusBridge {
  public static final String TOPIC = "/Telemetry/TestHub/Status";
  private static final Gson JSON = new Gson();
  private static final PrintWriter OUT = new PrintWriter(System.out, true, StandardCharsets.UTF_8);

  private static void emit(Map<String, ?> event) {
    OUT.println(JSON.toJson(event));
    if (OUT.checkError()) throw new IllegalStateException("status stdout closed");
  }

  public static void main(String[] args) throws Exception {
    if (args.length != 2 || args[0].isBlank() || args[0].length() > 253) {
      throw new IllegalArgumentException("Explicit host and port required");
    }
    int port = Integer.parseInt(args[1]);
    if (port < 1 || port > 65535) throw new IllegalArgumentException("Invalid explicit port");
    var probes = new ArrayBlockingQueue<Long>(2);
    Thread input = new Thread(() -> {
      try (var reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
        var line = new StringBuilder();
        for (int c; (c = reader.read()) != -1;) {
          if (c == '\n') {
            JsonObject request = JsonParser.parseString(line.toString()).getAsJsonObject();
            if (!request.get("type").getAsString().equals("clock_probe")) return;
            long id = request.get("id").getAsLong();
            if (id < 0 || !probes.offer(id)) return;
            line.setLength(0);
          } else {
            if (line.length() >= 512) return;
            line.append((char)c);
          }
        }
      } catch (Exception ignored) { /* No input publication can affect robot state. */ }
    }, "status-clock-probes");
    input.setDaemon(true);
    input.start();
    try (var instance = NetworkTableInstance.create();
         StringSubscriber subscriber = instance.getStringTopic(TOPIC).subscribe("",
             PubSubOption.SEND_ALL, PubSubOption.KEEP_DUPLICATES, PubSubOption.DISABLE_LOCAL,
             PubSubOption.pollStorage(1), PubSubOption.periodic(.02))) {
      instance.addLogger(0, 100, event -> {}); // Native diagnostics cannot contaminate stdout.
      AtomicLong disconnects = new AtomicLong();
      instance.addConnectionListener(true, event -> {
        if (event.is(NetworkTableEvent.Kind.DISCONNECTED)) disconnects.incrementAndGet();
      });
      instance.setServer(args[0], port);
      instance.startClient("robot-test-hub-status-alpha7");
      emit(Map.of("type", "ready", "protocol", 1, "profile", "wpilib-2027.0.0-alpha-7-windowsx86-64",
                  "topic", TOPIC));
      boolean connected = false;
      long connectionEpoch = 0, observedDisconnects = 0, lastLocal = 0, lastServer = 0;
      while (!Thread.currentThread().isInterrupted()) {
        Long probe;
        while ((probe = probes.poll()) != null) {
          emit(Map.of("type", "clock_probe", "id", probe, "nt_local_ns", Long.toString(WPIUtilJNI.now())));
        }
        long losses = disconnects.get();
        if (losses != observedDisconnects) {
          observedDisconnects = losses;
          connected = false;
          lastLocal = lastServer = 0;
          subscriber.readQueue();
          emit(Map.of("type", "disconnected", "connection_epoch", connectionEpoch));
        }
        boolean live = instance.isConnected();
        if (live != connected) {
          connected = live;
          lastLocal = lastServer = 0;
          if (live) connectionEpoch++;
          emit(Map.of("type", live ? "connected" : "disconnected", "connection_epoch", connectionEpoch));
        }
        TimestampedString[] values = subscriber.readQueue();
        if (connected && values.length > 0) {
          TimestampedString value = values[values.length - 1];
          if (value.timestamp <= lastLocal || value.serverTime <= lastServer || value.serverTime <= 1
              || value.value.getBytes(StandardCharsets.UTF_8).length > 16384) {
            emit(Map.of("type", "rejected", "code", "publication_timestamp_or_size_invalid"));
          } else {
            lastLocal = value.timestamp;
            lastServer = value.serverTime;
            emit(Map.of("type", "publication", "connection_epoch", connectionEpoch,
                "nt_local_ns", Long.toString(value.timestamp), "nt_server_ns", Long.toString(value.serverTime),
                "payload", value.value));
          }
        }
        Thread.sleep(20);
      }
    }
  }
}
