"""Offline backend-I/O, resumption and actual compressed Zarr v3 tests."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr
from xarray.backends.common import BackendArray
from xarray.core import indexing

from point_checkpoint import PointCheckpoint, _identity, atomic_json, _digest
from point_reader import PointRequest, P50_FIELDS, select_point, main, read_gcs_point
from test_point_reader import fake_dataset, SYNOPTIC


class TracedBackend(BackendArray):
    def __init__(self, data, field, log, fail_lead=None):
        self.data = data
        self.shape, self.dtype = data.shape, data.dtype
        self.field, self.log, self.fail_lead = field, log, fail_lead

    def __getitem__(self, key):
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.BASIC, self._get
        )

    def _get(self, key):
        # Assert at the actual lazy backend boundary, NOT on an eager result.
        assert all(isinstance(k, (int, np.integer)) for k in key), key
        self.log.append((self.field, int(key[0]) + 1, key))
        if int(key[0]) + 1 == self.fail_lead:
            raise TimeoutError("synthetic backend timeout")
        return self.data[key]


def lazy_dataset(hours=24, fail_lead=None, source=None):
    source = fake_dataset(hours=hours) if source is None else source
    log = []
    variables = {}
    for field in source.data_vars:
        array = TracedBackend(source[field].values, field, log,
                              fail_lead if field == 'wind_speed_10m_p50' else None)
        variables[field] = xr.Variable(source[field].dims,
                                      indexing.LazilyIndexedArray(array),
                                      attrs=source[field].attrs)
    return xr.Dataset(variables, coords=source.coords), log


def request(hours=24, all_fields=False, lon=-0.1):
    return PointRequest(.1, lon, SYNOPTIC, hours, not all_fields)


def identity():
    return {'hours':24, 'available_fields':list(P50_FIELDS), 'run':'synthetic'}


class DirectCheckpointTests(unittest.TestCase):
    def test_24h_direct_reads_only_scalar_backend_keys(self):
        ds, log = lazy_dataset()
        req = request()
        actual = select_point(ds, req, fetch_mode='direct')
        expected = select_point(fake_dataset(hours=24), req)
        self.assertEqual(actual, expected)
        self.assertEqual(len(log), 24*4)
        self.assertTrue(all(k[2][1:] == (1,1) for k in log))

    def test_360_hours_all_percentiles_preserve_canonical_values(self):
        ds, log = lazy_dataset(hours=360)
        req = request(360, all_fields=True)
        actual = select_point(ds, req, fetch_mode='direct')
        self.assertEqual(actual, select_point(fake_dataset(hours=360), req))
        self.assertEqual(len(log), 360*12)

    def test_full_quantiles_reuse_all_24_hours_of_p50_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            p50_dataset, p50_reads = lazy_dataset(hours=24)
            select_point(p50_dataset, request(), fetch_mode='direct',
                         checkpoint_dir=directory)
            self.assertEqual(len(p50_reads), 24 * 4)

            full_dataset, native_reads = lazy_dataset(hours=24)
            messages = []
            actual = select_point(
                full_dataset, request(all_fields=True),
                fetch_mode='direct', checkpoint_dir=directory,
                progress=messages.append,
            )
            self.assertEqual(
                actual, select_point(fake_dataset(hours=24), request(all_fields=True))
            )
            self.assertEqual(len(native_reads), 24 * 8)
            self.assertTrue(all(not field.endswith('_p50')
                                for field, _, _ in native_reads))
            self.assertTrue(any("REUSE_P50: 96 validated" in msg
                                for msg in messages))
            self.assertTrue(any("96/288 source scalars retained" in msg
                                for msg in messages))
            self.assertEqual(len(list(Path(directory).glob('*.checkpoint.json'))), 2)

            repeat, calls = lazy_dataset(hours=24)
            self.assertEqual(
                select_point(repeat, request(all_fields=True),
                             fetch_mode='direct', checkpoint_dir=directory),
                actual,
            )
            self.assertEqual(calls, [])

    def test_seed_does_not_cross_sites_or_nonmatching_units(self):
        with tempfile.TemporaryDirectory() as directory:
            select_point(lazy_dataset(hours=3)[0], request(3),
                         fetch_mode='direct', checkpoint_dir=directory)

            changed_site, reads = lazy_dataset(hours=3)
            select_point(changed_site, request(3, all_fields=True, lon=-.099),
                         fetch_mode='direct', checkpoint_dir=directory)
            self.assertEqual(len(reads), 3 * 12)

        with tempfile.TemporaryDirectory() as directory:
            select_point(lazy_dataset(hours=3)[0], request(3),
                         fetch_mode='direct', checkpoint_dir=directory)
            altered = fake_dataset(hours=3)
            altered["wind_speed_10m_p50"].attrs["units"] = "different-native-unit"
            full, reads = lazy_dataset(hours=3, source=altered)
            select_point(full, request(3, all_fields=True),
                         fetch_mode='direct', checkpoint_dir=directory)
            self.assertEqual(len(reads), 3 * 12)

    def test_conflicting_matching_checkpoint_fails_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            select_point(lazy_dataset(hours=3)[0], request(3, all_fields=True),
                         fetch_mode='direct', checkpoint_dir=directory)
            select_point(lazy_dataset(hours=3)[0], request(3),
                         fetch_mode='direct', checkpoint_dir=directory)

            for path in Path(directory).glob("*.checkpoint.json"):
                document = json.loads(path.read_text(encoding="utf-8"))
                if len(document["body"]["identity"]["requested_fields"]) == 12:
                    document["body"]["values"]["1"]["temperature_2m_p50"] += 1.0
                    document["sha256"] = _digest(document["body"])
                    atomic_json(path, document)
                    break
            else:
                self.fail("full-field checkpoint was not found")

            ds, reads = lazy_dataset(hours=3)
            with self.assertRaisesRegex(ValueError, "CHECKPOINT_CONFLICT"):
                select_point(ds, request(3, all_fields=True),
                             fetch_mode='direct', checkpoint_dir=directory)
            self.assertEqual(reads, [])

    def test_11_hours_survive_timeout_and_resume_without_rereading_them(self):
        req = request()
        with tempfile.TemporaryDirectory() as directory:
            broken, log = lazy_dataset(fail_lead=12)
            with self.assertRaises(TimeoutError):
                select_point(broken, req, fetch_mode='direct', checkpoint_dir=directory)
            path, = Path(directory).glob('*.checkpoint.json')
            saved = json.loads(path.read_text())['body']['values']
            self.assertTrue(all(set(P50_FIELDS).issubset(saved[str(h)]) for h in range(1,12)))
            self.assertNotIn('wind_speed_10m_p50', saved.get('12', {}))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            recovered, rereads = lazy_dataset()
            result = select_point(recovered, req, fetch_mode='direct', checkpoint_dir=directory)
            self.assertTrue(all(h >= 12 for _,h,_ in rereads))
            self.assertEqual(result, select_point(fake_dataset(hours=24), req))
            # A completely cached replay must do zero meteorological reads.
            again, calls = lazy_dataset()
            self.assertEqual(select_point(again, req, fetch_mode='direct', checkpoint_dir=directory), result)
            self.assertEqual(calls, [])

    def test_interrupted_output_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'forecast.json'
            atomic_json(output, {'previous':'complete'})
            before = output.read_bytes()
            with patch('point_reader.read_gcs_point', side_effect=TimeoutError):
                code = main(['--lat','0.1','--lon','-0.1','--hours','24',
                             '--run-prefix',SYNOPTIC,'--output',str(output)])
            self.assertEqual(code,1)
            self.assertEqual(output.read_bytes(),before)

    def test_nan_p50_fails_without_caching_invalid_value(self):
        source=fake_dataset(hours=3)
        source['total_cloud_cover_p50'].values[1,1,1]=np.nan
        ds, log = lazy_dataset(hours=3,source=source)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'INCOMPLETE_HOURLY_VALUES'):
                select_point(ds,request(3),fetch_mode='direct',checkpoint_dir=directory)
            path,=Path(directory).glob('*.checkpoint.json')
            values=json.loads(path.read_text())['body']['values']
            self.assertNotIn('total_cloud_cover_p50',values.get('2',{}))
            self.assertTrue(all(h <= 2 for _,h,_ in log))

    def test_zero_and_optional_null_are_preserved(self):
        source=fake_dataset(hours=3)
        source['surface_solar_radiation_downwards_1hr_p50'].values[0,1,1]=0
        source['total_cloud_cover_p90'].values[1,1,1]=np.nan
        ds,_=lazy_dataset(hours=3,source=source)
        actual=select_point(ds,request(3,all_fields=True),fetch_mode='direct')
        self.assertEqual(actual,select_point(source,request(3,all_fields=True)))

    def test_different_site_cannot_reuse_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            ds,_=lazy_dataset(hours=3)
            select_point(ds,request(3),fetch_mode='direct',checkpoint_dir=directory)
            ds,log=lazy_dataset(hours=3)
            select_point(ds,request(3,lon=-.099),fetch_mode='direct',checkpoint_dir=directory)
            self.assertEqual(len(log),12)
            self.assertEqual(len(list(Path(directory).glob('*.checkpoint.json'))),2)

    def test_checksum_failure_blocks_before_forecast_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            ds,_=lazy_dataset(hours=3)
            select_point(ds,request(3),fetch_mode='direct',checkpoint_dir=directory)
            path,=Path(directory).glob('*.checkpoint.json')
            obj=json.loads(path.read_text()); obj['sha256']='bad'
            path.write_text(json.dumps(obj))
            ds,log=lazy_dataset(hours=3)
            with self.assertRaisesRegex(ValueError,'checksum'):
                select_point(ds,request(3),fetch_mode='direct',checkpoint_dir=directory)
            self.assertEqual(log,[])

    def test_identity_and_invalid_cached_values_are_checked_even_with_valid_hash(self):
        for modification in ('identity','bool','nan_p50'):
            with self.subTest(modification=modification), tempfile.TemporaryDirectory() as directory:
                with PointCheckpoint(directory,identity(),P50_FIELDS) as cp:
                    cp.put(1,P50_FIELDS[0],280.0); path=cp.path
                obj=json.loads(path.read_text())
                if modification=='identity': obj['body']['identity']['run']='another-run'
                elif modification=='bool': obj['body']['values']['1'][P50_FIELDS[0]]=True
                else: obj['body']['values']['1'][P50_FIELDS[0]]=None
                obj['sha256']=_digest(obj['body']); path.write_text(json.dumps(obj))
                with self.assertRaises(ValueError):
                    with PointCheckpoint(directory,identity(),P50_FIELDS): pass

    def test_two_writers_cannot_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            with PointCheckpoint(directory,identity(),P50_FIELDS):
                with self.assertRaises(BlockingIOError):
                    with PointCheckpoint(directory,identity(),P50_FIELDS): pass

    def test_insecure_permissions_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with PointCheckpoint(directory,identity(),P50_FIELDS) as cp:
                cp.put(1,P50_FIELDS[0],280.0); path=cp.path
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                with PointCheckpoint(directory,identity(),P50_FIELDS): pass
            path.unlink(); target=Path(directory)/'target'; target.write_text('unchanged')
            path.symlink_to(target)
            with self.assertRaises(OSError):
                with PointCheckpoint(directory,identity(),P50_FIELDS): pass
            self.assertEqual(target.read_text(),'unchanged')

    def test_sigterm_keeps_acknowledged_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            code = '''import sys,time
from point_checkpoint import PointCheckpoint
from point_reader import P50_FIELDS
identity={'hours':24,'available_fields':list(P50_FIELDS),'run':'synthetic'}
with PointCheckpoint(sys.argv[1],identity,P50_FIELDS) as cp:
 for lead in range(1,12):
  for field in P50_FIELDS: cp.put(lead,field,1.0)
 print('PERSISTED',flush=True)
 time.sleep(60)
'''
            child=subprocess.Popen([sys.executable,'-c',code,directory],stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(),'PERSISTED')
                child.send_signal(signal.SIGTERM); child.wait(timeout=5)
                with PointCheckpoint(directory,identity(),P50_FIELDS) as cp:
                    self.assertEqual(cp.completed_hours(),11)
            finally:
                if child.poll() is None: child.kill(); child.wait()
                child.stdout.close()

    def test_checkpoint_cannot_be_used_with_legacy_modes(self):
        with self.assertRaisesRegex(ValueError,'checkpoints require direct'):
            select_point(fake_dataset(hours=3),request(3),checkpoint_dir='/unused')

    def test_direct_opener_and_retry_options_without_credentials(self):
        # Uses mocks only; no ambient credential lookup, network or fake auth.
        from types import ModuleType
        ob=ModuleType('obstore'); store=ModuleType('obstore.store')
        za=ModuleType('zarr'); storage=ModuleType('zarr.storage')
        from unittest.mock import MagicMock
        store.GCSStore=MagicMock(return_value=object())
        storage.ObjectStore=MagicMock(return_value=object())
        ds=fake_dataset(hours=3)
        with patch.dict(sys.modules,{'obstore':ob,'obstore.store':store,'zarr':za,'zarr.storage':storage}):
            with patch('xarray.open_zarr',return_value=ds) as opened:
                out=read_gcs_point(request(3),fetch_mode='direct')
        self.assertIsNone(opened.call_args.kwargs['chunks'])
        from datetime import timedelta
        self.assertEqual(store.GCSStore.call_args.kwargs['retry_config'],
                         {'max_retries':2,'retry_timeout':timedelta(seconds=60)})
        self.assertEqual(out['returned_hours'],3)


HAS_ZARR = importlib.util.find_spec('zarr') is not None and importlib.util.find_spec('dask') is not None

@unittest.skipUnless(HAS_ZARR, 'Actual Zarr/Dask integration requires pinned CI dependencies')
class RealZarrPointTests(unittest.TestCase):
    def test_actual_compressed_sharded_v3_roundtrip_and_range_reads(self):
        from zarr.storage import MemoryStore
        store=MemoryStore(); source=fake_dataset(hours=24)
        encoding={f:{'chunks':(1,2,2),'shards':(24,2,2)} for f in source.data_vars}
        source.to_zarr(store,zarr_format=3,consolidated=False,encoding=encoding)
        with xr.open_zarr(store,chunks={},consolidated=False) as ds:
            expected=select_point(ds,request())
        log=[]; original_get=MemoryStore.get
        async def traced_get(self,key,prototype,byte_range=None):
            value=await original_get(self,key,prototype,byte_range)
            log.append((key,byte_range,len(value) if value is not None else 0))
            return value
        with tempfile.TemporaryDirectory() as directory, patch.object(MemoryStore,'get',traced_get):
            with xr.open_zarr(store,chunks=None,consolidated=False) as ds:
                log.clear()
                actual=select_point(ds,request(),fetch_mode='direct',checkpoint_dir=directory)
                data_reads=[r for r in log if '/c/' in r[0]]
                self.assertTrue(data_reads)
                self.assertTrue(any(r[1] is not None for r in data_reads))
            self.assertEqual(actual,expected)
            with xr.open_zarr(store,chunks=None,consolidated=False) as ds:
                log.clear()
                self.assertEqual(select_point(ds,request(),fetch_mode='direct',checkpoint_dir=directory),expected)
                self.assertFalse(any('/c/' in r[0] for r in log))

    def test_rejects_dask_array_in_direct_mode(self):
        with self.assertRaisesRegex(ValueError,'DIRECT_REQUIRES_NO_DASK'):
            select_point(fake_dataset(hours=3).chunk({'lead_time':1}),request(3),fetch_mode='direct')


if __name__=='__main__':
    unittest.main()
