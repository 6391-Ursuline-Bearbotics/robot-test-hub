"""Synthetic Parquet query timing; excludes WPILOG import/conversion."""
import argparse,json,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from robot_test_hub.columnar import connection,sql_path,DUCKDB_VERSION
from robot_test_hub.summaries import _window_query,TRACKING_SQL
from robot_test_hub.swerve import MODULE_POSITIONS
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--samples",type=int,default=500000)
parser.add_argument("--threads",type=int,default=1)
parser.add_argument("--memory-mb",type=int,default=512)
args=parser.parse_args()
if args.samples<8 or args.samples>10000000 or args.samples%4:parser.error("samples must be a multiple of four between 8 and 10000000")
with tempfile.TemporaryDirectory(prefix="rth-analytics-benchmark-") as name:
    root=Path(name);parquet=root/"synthetic.parquet"
    started=time.perf_counter()
    with connection(root,threads=args.threads,memory_mb=args.memory_mb) as duck:
        positions="["+",".join(sql_path(p) for p in MODULE_POSITIONS)+"]"
        duck.execute("""COPY (SELECT 'synthetic' AS import_job_id,'synthetic' AS source_sha256,
            1000000000+(i//4)*20000000 AS timestamp_ns,i//4 AS cycle_record_index,
            (i%4)::INTEGER AS module_index,"""+positions+"""[(i%4)+1] AS module_position,
            'SIM' AS runtime_mode,TRUE AS enabled,TRUE AS mapping_valid,
            TRUE AS connected,TRUE AS drive_connected,TRUE AS turn_connected,
            2.0 AS command_speed_mps,2.0+SIN(i/100.0)*.2 AS measured_speed_mps,
            0.0 AS command_angle_rad,SIN(i/80.0)*.05 AS measured_angle_rad,
            1000000000+(i//4)*20000000 AS command_cycle_ns,
            1000000000+(i//4)*20000000 AS measured_cycle_ns,
            10.0 AS drive_current_amps,2.0 AS turn_current_amps,
            1000000000+(i//4)*20000000 AS drive_current_cycle_ns,
            1000000000+(i//4)*20000000 AS turn_current_cycle_ns
            FROM range(?) t(i)) TO """+sql_path(parquet)+" (FORMAT PARQUET,COMPRESSION ZSTD)",[args.samples])
    preparation=time.perf_counter()-started
    timings=[]
    for attempt in range(3):
        started=time.perf_counter()
        with connection(root,threads=args.threads,memory_mb=args.memory_mb) as duck:
            duck.execute("CREATE TEMP VIEW intervals AS "+_window_query([parquet],0,2**63-1,
                {"max_gap_ns":250000000,"max_sample_age_ns":100000000,"min_demand_mps":.1}))
            result=duck.execute(TRACKING_SQL).fetchall()
        timings.append(time.perf_counter()-started)
    print(json.dumps({"source":"invented_synthetic_frame_values","duckdb_version":DUCKDB_VERSION,
        "module_samples":args.samples,"robot_cycles":args.samples//4,"threads":args.threads,
        "duckdb_memory_limit_mb":args.memory_mb,"parquet_bytes":parquet.stat().st_size,
        "synthetic_preparation_seconds":preparation,"weighted_tracking_query_seconds":timings,
        "result_modules":len(result),"operating_system_cache_not_flushed":True,
        "wpilog_import_conversion_and_catalog_time_included":False},indent=2))
