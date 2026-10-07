"""Opt-in actual generated media checks; no camera or robot connection."""
from decimal import Decimal
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from robot_test_hub.video_media import MediaToolsConfig, NativeMediaAdapter

FFMPEG=os.environ.get('ROBOT_HUB_FFMPEG')
FFPROBE=os.environ.get('ROBOT_HUB_FFPROBE')
VERSION=os.environ.get('ROBOT_HUB_FFMPEG_VERSION','9.0.2-essentials_build-www.gyan.dev')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(arguments):
    return subprocess.run(arguments,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        timeout=20,shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout


def generated_original(folder):
    path=folder/'segment-000000.mkv'
    # Independent, lossless black/white frames, including a nonzero source PTS.
    source=("color=c=black:s=32x32:r=10,drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
            "enable='eq(mod(n,10),1)+eq(mod(n,10),5)+eq(mod(n,10),9)'")
    run([FFMPEG,'-hide_banner','-loglevel','error','-n','-f','lavfi','-i',source,
        '-vf',"select='not(eq(mod(n,10),2)+eq(mod(n,10),6))',setpts=PTS+5/TB",
        '-frames:v','8','-fps_mode','passthrough','-c:v','ffv1',str(path)])
    observation=json.loads(run([FFPROBE,'-v','error','-select_streams','v:0','-show_frames',
        '-show_entries','frame=best_effort_timestamp_time,duration_time,pkt_duration_time',
        '-of','json',str(path)]))
    frames=[(int(Decimal(f['best_effort_timestamp_time'])*1000000000),
             int(Decimal(f.get('duration_time',f.get('pkt_duration_time')))*1000000000))
             for f in observation['frames']]
    return path,frames


@unittest.skipUnless(FFMPEG and FFPROBE,'Explicit opt-in FFmpeg/FFprobe paths required')
class NativeMediaTests(unittest.TestCase):
    def tools(self):
        return MediaToolsConfig(FFMPEG,FFPROBE,VERSION,digest(FFMPEG),digest(FFPROBE),
            operation_timeout=20,minimum_free_bytes=1048576)

    def test_verified_mp4_groups_keep_nonzero_original_pts_cues_and_durations(self):
        with tempfile.TemporaryDirectory(prefix='hub-native-media-') as directory:
            folder=Path(directory);original,source_frames=generated_original(folder)
            expected_pts=[5000000000+n*100000000 for n in (0,1,3,4,5,7,8,9)]
            self.assertEqual(source_frames,[(p,100000000) for p in expected_pts])
            original_sha=digest(original)
            adapter=NativeMediaAdapter(self.tools());stop=threading.Event()
            adapter.validate(stop)
            # Real missing exposure intervals split clips; time is never compressed.
            groups=((1,1),(2,4),(5,7))
            expected_cues=([0],[2],[2])
            for group,(first,last) in enumerate(groups):
                output=folder/f'clip-{group}.mp4'
                adapter.render(original,first,last,output,stop)
                observed=adapter.probe(output,stop)
                self.assertEqual(observed,[(source_frames[i][0]-source_frames[first][0],100000000)
                                           for i in range(first,last+1)])
                pixels=run([FFMPEG,'-v','error','-i',str(output),'-map','0:v:0',
                    '-fps_mode','passthrough','-pix_fmt','gray','-f','rawvideo','pipe:1'])
                self.assertEqual(len(pixels),(last-first+1)*32*32)
                means=[sum(pixels[i*1024:(i+1)*1024])/1024 for i in range(last-first+1)]
                self.assertEqual([i for i,mean in enumerate(means) if mean>200],expected_cues[group])
                self.assertEqual(digest(original),original_sha)

    def test_actual_http_preservation_downloads_and_tools_disabled_restart(self):
        from robot_test_hub.collector import RobotStatus
        from robot_test_hub.config import Config
        from robot_test_hub.server import create_http_server
        from robot_test_hub.service import HubService

        class QuietSource:
            source_type='generated_native_fixture'
            def status(self):return RobotStatus(True,time.monotonic(),'fixture-status-boot',1,False)
            def list_closed_files(self):raise AssertionError('Enabled fixture forbids source reads')
            def read(self,*args,**kwargs):raise AssertionError('Enabled fixture forbids source reads')

        with tempfile.TemporaryDirectory(prefix='hub-native-media-http-') as directory:
            root=Path(directory)/'hub'
            folder=root/'video'/'generated'/'session'/'capture'
            folder.mkdir(parents=True)
            original,frames=generated_original(folder);source_sha=digest(original)
            manifest=dict(schema_version=1,segment_id='capture-000000',camera_id='generated',
                session_id='session',capture_id='capture',relative_path=original.name,
                source_type='SYNTHETIC',state='closed_verified',sha256=source_sha,
                size_bytes=original.stat().st_size,container='matroska',codec='ffv1',time_base='1/1000',
                start_pts_ns=str(frames[0][0]),end_pts_ns=str(sum(frames[-1])),
                frames=[dict(pts_ns=str(p),duration_ns=str(d)) for p,d in frames],
                utc_basis='unmapped_recording_pts',utc_uncertainty_ns=None,provenance={})
            original.with_name(original.name+'.json').write_text(json.dumps(manifest),encoding='utf-8')
            service=None;server=None;thread=None
            def start(tools):
                nonlocal service,server,thread
                source=QuietSource()
                service=HubService(Config(data_dir=str(root)),source,video_media_config=tools)
                service.start();server=create_http_server(service,source,0)
                thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
                thread.start()
            def close():
                nonlocal service,server,thread
                if server is not None:
                    server.shutdown();server.server_close();thread.join(3)
                    self.assertFalse(thread.is_alive());server=None
                if service is not None:
                    self.assertTrue(service.close());service=None
            def request(method,path,payload=None,headers=None):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                try:
                    connection.request(method,path,None if payload is None else json.dumps(payload),
                        headers=headers or {'Content-Type':'application/json','X-Hub-Request':'1'})
                    response=connection.getresponse()
                    return response.status,dict(response.getheaders()),response.read()
                finally:connection.close()
            def wait(path,predicate):
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    status,_,body=request('GET',path)
                    if status==200 and predicate(json.loads(body)):return json.loads(body)
                    time.sleep(.03)
                self.fail('Generated media qualification did not reach expected state: '+body.decode())
            try:
                start(self.tools())
                wait('/api/v1/video/media-tools',lambda data:data['state']=='ready')
                wait('/api/v1/video',lambda data:data['recovery']['verified_segments']==1)
                base=9007199254740993
                def anchor(index,robot):
                    return dict(robot_ns=str(robot),segment_id='capture-000000',source_sha256=source_sha,
                        frame_index=index,robot_uncertainty_ns='0',video_uncertainty_ns='1',
                        evidence_label='Generated cue; explicit fixture timing assumption')
                calibration=dict(alignment_id='native-media-alignment',revision=1,expected_previous_sha256=None,
                    robot_id='generated-robot',boot_id='generated-boot',camera_id='generated',session_id='session',
                    capture_id='capture',segment_ids=['capture-000000'],windows=[dict(continuity_id='continuous',
                        start_robot_ns=str(base+99000000),end_robot_ns=str(base+891000000),
                        segment_ids=['capture-000000'],model_uncertainty_ns='0',
                        anchors=[anchor(1,base+99000000),anchor(7,base+891000000)],
                        assumed_scale=None,assumed_drift_uncertainty_ppm=None)])
                status,_,body=request('POST','/api/v1/video/alignments',calibration)
                self.assertEqual(status,200,body);receipt=json.loads(body)
                payload=dict(request_id='native-media-request',selection=dict(kind='interval',candidate_index=0,
                    alignment_id=receipt['alignment_id'],revision=receipt['revision'],sha256=receipt['sha256'],
                    robot_id='generated-robot',boot_id='generated-boot',start_robot_ns=str(base+99000000),
                    end_robot_ns=str(base+891000000),event_id=None,note_revision=None))
                status,_,body=request('POST','/api/v1/video/preservations',payload)
                self.assertEqual(status,200,body)
                ready=wait('/api/v1/video/preservations/native-media-request',lambda data:data['state'] in ('ready','failed'))
                self.assertEqual(ready['state'],'ready',ready)
                self.assertEqual([item['frame_count'] for item in ready['items']],[2,3,3])
                selected=[]
                for item in ready['items']:
                    route='/api/v1/video/media/'+item['item_id']
                    status,headers,clip=request('GET',route)
                    self.assertEqual(status,200);self.assertEqual(headers['Content-Type'],'video/mp4')
                    self.assertEqual(hashlib.sha256(clip).hexdigest(),item['sha256'])
                    self.assertEqual(request('HEAD',route)[2],b'')
                    status,headers,partial=request('GET',route,headers={'Range':'bytes=0-15'})
                    self.assertEqual(status,206);self.assertEqual(partial,clip[:16])
                    status,_,body=request('GET',route+'/sidecar')
                    self.assertEqual(status,200);sidecar=json.loads(body)
                    self.assertFalse(sidecar['measured_camera_alignment'])
                    self.assertEqual(sidecar['source_sha256'],source_sha)
                    selected.extend(frame['source_frame_index'] for frame in sidecar['frames'])
                    downloaded=Path(directory)/(item['item_id']+'.mp4');downloaded.write_bytes(clip)
                    observed=NativeMediaAdapter(self.tools()).probe(downloaded,threading.Event())
                    self.assertEqual(observed,[(int(f['derived_pts_ns']),int(f['duration_ns'])) for f in sidecar['frames']])
                self.assertEqual(selected,list(range(8)))
                self.assertEqual(digest(original),source_sha)
                self.assertIsNone(service.video.recorder)
                close();start(None)
                recovered=wait('/api/v1/video/preservations/native-media-request',lambda data:data['state']=='ready')
                self.assertEqual(recovered,ready)
                route='/api/v1/video/media/'+ready['items'][0]['item_id']
                self.assertEqual(request('GET',route)[0],200)
                self.assertEqual(request('POST','/api/v1/video/preservations',payload)[0],503)
                self.assertEqual(digest(original),source_sha)
            finally:close()


if __name__=='__main__':unittest.main()
