"""Opt-in encoded lavfi cue qualification; no camera, robot or network source.

Raw decoded pixels establish the independently prescribed cue frame numbers.
The robot clock is an explicitly synthetic 99 ms tick per 100 ms video frame.
This is generated-media evidence, never measured physical camera alignment.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from robot_test_hub.recorder import FFmpegAdapter, FFmpegConfig, Recorder
from robot_test_hub.video_alignment import (AlignmentError, AlignmentRevisions, CalibrationWindow,
                                          ManualAlignment, ManualAnchor, VideoSegment)


FFMPEG=os.environ.get('ROBOT_HUB_FFMPEG')
FFPROBE=os.environ.get('ROBOT_HUB_FFPROBE')
VERSION=os.environ.get('ROBOT_HUB_FFMPEG_VERSION','9.0.2-essentials_build-www.gyan.dev')


@unittest.skipUnless(FFMPEG and FFPROBE,'Explicit opt-in FFmpeg and FFprobe paths required')
class NativeGeneratedAlignmentTests(unittest.TestCase):
    def _capture(self, recorder):
        recorder.start('generated-cue-session')
        deadline=time.monotonic()+15
        while not recorder.list_segments() and time.monotonic()<deadline:
            health=recorder.tick()
            self.assertNotEqual(health['state'],'error',health)
            time.sleep(.05)
        self.assertTrue(recorder.list_segments(),recorder.health())
        folder=recorder.folder
        self.assertEqual(recorder.stop()['state'],'stopped')
        self.assertIsNone(recorder.owner)
        self.assertIsNotNone(recorder.adapter.process.poll())
        return folder,recorder.list_segments()[0]

    def test_encoded_visual_cues_variable_pts_drift_gaps_and_immutable_restart_mappings(self):
        for executable in (FFMPEG,FFPROBE):
            self.assertTrue(Path(executable).is_absolute())
            self.assertTrue(Path(executable).is_file())
        with tempfile.TemporaryDirectory(prefix='robot-hub-generated-alignment-') as directory:
            # White cue flashes at source frame numbers 1, 5 and 9. Frames 2
            # and 6 are removed AFTER cue generation; remaining PTS stay intact.
            cue_source=("color=c=black:s=32x32:r=10,"
                "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
                "enable='eq(mod(n,10),1)+eq(mod(n,10),5)+eq(mod(n,10),9)',"
                "select='not(eq(mod(n,10),2)+eq(mod(n,10),6))'")
            config=FFmpegConfig(FFMPEG,FFPROBE,VERSION,'generated-cue-camera','lavfi',cue_source,
                               'SYNTHETIC',segment_seconds=1,stalled_seconds=10,
                               minimum_free_bytes=1024*1024,operation_timeout=15)
            adapter=FFmpegAdapter(config)
            recorder=Recorder(Path(directory)/'video',config,adapter=adapter)
            self.addCleanup(recorder.stop)
            folder,manifest=self._capture(recorder)
            original=folder/manifest['relative_path']
            original_bytes=original.read_bytes()
            self.assertEqual(hashlib.sha256(original_bytes).hexdigest(),manifest['sha256'])
            self.assertEqual(manifest['source_type'],'SYNTHETIC')
            self.assertEqual(manifest['utc_basis'],'unmapped_recording_pts')
            self.assertIsNone(manifest['utc_uncertainty_ns'])
            source_numbers=(0,1,3,4,5,7,8,9)
            expected_pts=[number*100_000_000 for number in source_numbers]
            probed=adapter.probe(original)
            self.assertEqual([frame['pts_ns'] for frame in probed['frames']],expected_pts)
            self.assertEqual([int(frame['pts_ns']) for frame in manifest['frames']],expected_pts)
            self.assertTrue(all(frame['duration_ns']==100_000_000 for frame in probed['frames']))
            decoded=subprocess.run([FFMPEG,'-hide_banner','-v','error','-i',str(original),'-map','0:v:0',
                '-fps_mode','passthrough','-f','rawvideo','-pix_fmt','gray','pipe:1'],check=True,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15,shell=False,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout
            pixels=32*32
            self.assertEqual(len(decoded),len(source_numbers)*pixels)
            means=[sum(decoded[index*pixels:(index+1)*pixels])/pixels for index in range(len(source_numbers))]
            cue_indexes=[index for index,mean in enumerate(means) if mean>200]
            self.assertEqual(cue_indexes,[1,4,7])
            self.assertTrue(all(mean<30 for index,mean in enumerate(means) if index not in cue_indexes))
            evidence=VideoSegment.from_manifest(folder,manifest)
            robot_origin=9_007_199_254_740_993
            anchors=tuple(ManualAnchor(robot_origin+source_numbers[index]*99_000_000,
                evidence.segment_id,evidence.sha256,index,0,10_000_000,
                'generated white cue at source frame '+str(source_numbers[index])) for index in cue_indexes)
            window=CalibrationWindow('generated-continuous',anchors[0].robot_ns,anchors[-1].robot_ns,
                                     anchors,(evidence.segment_id,))
            def make_mapping(segment, calibration):
                return ManualAlignment('synthetic-robot','synthetic-boot',segment.camera_id,segment.session_id,
                                       segment.capture_id,(segment,),(calibration,))
            mapping=make_mapping(evidence,window)
            fit=mapping.document()['windows'][0]
            self.assertEqual(fit['scale'],{'numerator':'100','denominator':'99'})
            self.assertEqual(fit['residuals_ns'],['0','0','0'])
            self.assertFalse(mapping.document()['utc_launch_used'])
            self.assertFalse(mapping.document()['measured_camera_alignment'])
            def query(offset,end=None):
                return mapping.map_interval('synthetic-robot','synthetic-boot',robot_origin+offset,
                    robot_origin+(offset if end is None else end),event_id='generated-incident',run_id='generated-run')
            point=query(445_500_000)  # Independently prescribed PTS 450 ms.
            self.assertEqual(point['state'],'mapped')
            self.assertEqual(point['spans'][0]['frame_indexes'],[3])
            boundary=query(495_000_000)  # PTS 500 ms +/- supplied cue uncertainty.
            self.assertEqual(boundary['spans'][0]['frame_indexes'],[3,4])
            self.assertGreaterEqual(int(boundary['windows'][0]['uncertainty_ns']),10_000_000)
            self.assertEqual(query(247_500_000)['state'],'gap')  # PTS 250 ms was dropped.
            interval=query(148_500_000,841_500_000)  # PTS 150 through 850 ms.
            self.assertEqual(interval['state'],'partial')
            self.assertEqual(interval['spans'][0]['frame_indexes'],[1,2,3,4,5,6])
            self.assertEqual(interval['windows'][0]['unavailable_pts_intervals'],
                             [['200000000','300000000'],['600000000','700000000']])
            store=AlignmentRevisions(Path(directory)/'alignments','generated-alignment')
            first=store.append(1,mapping)
            saved=Path(first['path']).read_bytes()
            loaded=store.load(1,(evidence,),expected_sha256=first['sha256'])
            self.assertEqual(loaded.document(),mapping.document())
            self.assertEqual(loaded.map_interval('synthetic-robot','synthetic-boot',
                robot_origin+445_500_000,robot_origin+445_500_000)['spans'][0]['frame_indexes'],[3])
            changed=replace(window,anchors=tuple(replace(anchor,evidence_label=anchor.evidence_label+' reviewed')
                                                 for anchor in anchors))
            second=store.append(2,make_mapping(evidence,changed),expected_previous_sha256=first['sha256'])
            self.assertNotEqual(first['sha256'],second['sha256'])
            self.assertEqual(Path(first['path']).read_bytes(),saved)
            self.assertEqual(store.load(2,(evidence,),expected_sha256=second['sha256']).document(),
                             make_mapping(evidence,changed).document())
            # Restart the real encoder. PTS resets, but capture identity differs.
            restart_folder,restarted=self._capture(recorder)
            self.assertNotEqual(restarted['capture_id'],manifest['capture_id'])
            self.assertEqual(int(restarted['frames'][0]['pts_ns']),0)
            restart_evidence=VideoSegment.from_manifest(restart_folder,restarted)
            with self.assertRaises(AlignmentError):
                ManualAlignment('synthetic-robot','synthetic-boot',evidence.camera_id,evidence.session_id,
                    evidence.capture_id,(evidence,restart_evidence),(window,))
            self.assertEqual(mapping.map_interval('synthetic-robot','other-boot',robot_origin,robot_origin)['state'],
                             'unavailable')
            self.assertEqual(original.read_bytes(),original_bytes)
            self.assertIsNotNone(adapter.process.poll())
            print('GENERATED_ALIGNMENT_QUALIFICATION '+json.dumps({
                'profile':'lavfi-black-white-cues-1-5-9-drop-2-6-10hz-v1','version':VERSION,
                'provenance':manifest['provenance'],'first_original_pts_ns':expected_pts,
                'decoded_cue_frame_indexes':cue_indexes,'synthetic_scale':fit['scale'],
                'drift_ppm':fit['drift_ppm'],'residuals_ns':fit['residuals_ns'],
                'source_sha256':manifest['sha256'],'alignment_revision_sha256':second['sha256'],
                'native_generated_media_only':True,'measured_camera_alignment':False},sort_keys=True))


if __name__=='__main__':
    unittest.main()
