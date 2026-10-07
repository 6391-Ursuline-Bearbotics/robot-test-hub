import com.google.gson.Gson;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.littletonrobotics.junction.LogTable;
import org.littletonrobotics.junction.wpilog.WPILOGReader;
import org.wpilib.datalog.DataLogReader;
import org.wpilib.datalog.DataLogRecord;
import org.wpilib.math.geometry.Pose2d;
import org.wpilib.math.kinematics.SwerveModuleVelocity;
import org.wpilib.util.struct.StructBuffer;

/** Independent reader executable: never imports or invokes the fixture generator. */
public final class OfficialReader {
  public static void main(String[] args) throws Exception {
    DataLogReader reader = new DataLogReader(args[0]);
    if (!reader.isValid()) throw new IllegalArgumentException("Invalid WPILOG header");
    Map<Integer, DataLogRecord.StartRecordData> entries = new LinkedHashMap<>();
    List<Object> records = new ArrayList<>();
    // Alpha7 iterator.hasNext() conservatively requires 16 bytes. forEach reads valid short tails.
    List<DataLogRecord> completeRecords = new ArrayList<>();
    reader.forEach(completeRecords::add);
    for (DataLogRecord r : completeRecords) {
      Map<String, Object> row = new LinkedHashMap<>();
      row.put("timestamp_ns", r.getTimestamp());
      if (r.isStart()) {
        var s = r.getStartData(); entries.put(s.entry, s);
        row.put("control", "start"); row.put("entry", s.entry); row.put("name", s.name);
        row.put("type", s.type); row.put("metadata", s.metadata);
      } else if (r.isSetMetadata()) {
        var s = r.getSetMetadataData(); row.put("control", "metadata");
        row.put("name", entries.get(s.entry).name); row.put("metadata", s.metadata);
      } else if (r.isControl()) {
        row.put("control", "other");
      } else {
        var e = entries.get(r.getEntry());
        if (e == null) throw new IllegalStateException("Unregistered entry");
        row.put("name", e.name); row.put("type", e.type);
        Object value = switch (e.type) {
          case "int64" -> r.getInteger();
          case "double" -> r.getDouble();
          case "boolean" -> r.getBoolean();
          case "string" -> r.getString();
          case "double[]" -> r.getDoubleArray();
          case "struct:Pose2d" -> pose(StructBuffer.create(Pose2d.struct).read(r.getRaw()));
          case "struct:Pose2d[]" -> {
            List<Object> poses = new ArrayList<>();
            for (Pose2d p : StructBuffer.create(Pose2d.struct).readArray(r.getRaw())) poses.add(pose(p));
            yield poses;
          }
          case "struct:SwerveModuleVelocity[]" -> {
            List<Object> modules = new ArrayList<>();
            for (SwerveModuleVelocity module :
                StructBuffer.create(SwerveModuleVelocity.struct).readArray(r.getRaw())) {
              modules.add(Map.of("velocity_mps", module.velocity,
                  "angle_rad", module.angle.getRadians()));
            }
            yield modules;
          }
          case "structschema" -> new String(r.getRaw(), java.nio.charset.StandardCharsets.UTF_8);
          default -> HexFormat.of().formatHex(r.getRaw());
        };
        row.put("value", value);
      }
      records.add(row);
    }
    WPILOGReader replay = new WPILOGReader(args[0]);
    replay.start();
    LogTable table = new LogTable(0);
    List<Object> cycles = new ArrayList<>();
    boolean more;
    do {
      more = replay.updateTable(table); // EOF still updates the final table; capture it.
      Map<String, Object> cycle = new LinkedHashMap<>();
      cycle.put("timestamp_ns", table.getTimestamp());
      cycle.put("sequence", table.get("Fixture/Sequence", -1L));
      cycle.put("real_speed", table.get("RealOutputs/Drive/RequestedSpeed", Double.NaN));
      cycle.put("has_replay_output", table.get("ReplayOutputs/Drive/RequestedSpeed") != null);
      cycle.put("mode", table.get("Fixture/Mode", "MISSING"));
      cycles.add(cycle);
    } while (more);
    Map<String, Object> result = new LinkedHashMap<>();
    result.put("reader", "org.wpilib.datalog.DataLogReader 2027.0.0-alpha-7");
    result.put("record_header_unit_on_disk", "microseconds");
    result.put("reader_timestamp_unit", "nanoseconds");
    result.put("version", (int) reader.getVersion());
    result.put("extra_header", reader.getExtraHeader());
    result.put("records", records); result.put("advantagekit_replay_cycles", cycles);
    int iteratorCount = 0;
    for (DataLogRecord ignored : reader) iteratorCount++;
    result.put("iterator_record_count", iteratorCount);
    result.put("foreach_record_count", records.size());
    System.out.println(new Gson().toJson(result));
  }

  private static Map<String, Double> pose(Pose2d p) {
    return Map.of("x_m", p.getX(), "y_m", p.getY(), "heading_rad", p.getRotation().getRadians());
  }
}
