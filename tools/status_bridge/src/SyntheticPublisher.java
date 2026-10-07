import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import org.wpilib.networktables.NetworkTableInstance;
import org.wpilib.networktables.PubSubOption;

/** Qualification harness only: publishes explicit synthetic statuses on loopback. */
public final class SyntheticPublisher {
  public static void main(String[] args) throws Exception {
    if (args.length != 1) throw new IllegalArgumentException("Explicit loopback port required");
    int port = Integer.parseInt(args[0]);
    if (port < 1 || port > 65535) throw new IllegalArgumentException("Invalid port");
    try (var instance = NetworkTableInstance.create();
         var publisher = instance.getStringTopic(StatusBridge.TOPIC).publish(PubSubOption.KEEP_DUPLICATES);
         var input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
      instance.addLogger(0, 100, event -> {});
      instance.startServer("", "127.0.0.1", "", port);
      System.out.println("{\"type\":\"publisher_ready\"}");
      System.out.flush();
      for (String line; (line = input.readLine()) != null;) {
        if (line.length() > 20000) throw new IllegalArgumentException("Oversized synthetic request");
        JsonObject request = JsonParser.parseString(line).getAsJsonObject();
        switch (request.get("type").getAsString()) {
          case "publish" -> {
            long timestamp = request.has("nt_timestamp_ns") ? Long.parseLong(request.get("nt_timestamp_ns").getAsString()) : 0;
            publisher.set(request.get("payload").getAsString(), timestamp);
            instance.flush();
          }
          case "disconnect" -> instance.stopServer();
          case "reconnect" -> instance.startServer("", "127.0.0.1", "", port);
          case "stop" -> { return; }
          default -> throw new IllegalArgumentException("Unknown synthetic request");
        }
      }
    }
  }
}
