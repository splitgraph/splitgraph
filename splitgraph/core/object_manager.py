"""Functions related to creating, deleting and keeping track of physical Splitgraph objects."""
import itertools
import logging
import math
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime as dt

from psycopg2.sql import SQL, Identifier

from splitgraph.config import SPLITGRAPH_META_SCHEMA, CONFIG
from splitgraph.core.fragment_manager import FragmentManager
from splitgraph.core.metadata_manager import MetadataManager
from splitgraph.engine import ResultShape, switch_engine
from splitgraph.exceptions import SplitGraphError, ObjectCacheError
from splitgraph.hooks.external_objects import get_external_object_handler
from ._common import META_TABLES, select, insert, pretty_size, Tracer, CallbackList


class ObjectManager(FragmentManager, MetadataManager):
    """Brings the multiple manager classes together and manages the object cache (downloading and uploading
    objects as required in order to fulfill certain queries)"""

    def __init__(self, object_engine, metadata_engine=None):
        """
        :param object_engine: An ObjectEngine that will be used as a backing store for the
            objects.
        :param metadata_engine: An SQLEngine that will be used to store/query metadata for Splitgraph
            images and objects. By default, `object_engine` is used.
        """
        super().__init__(object_engine, metadata_engine or object_engine)

        # Cache size in bytes
        self.cache_size = float(CONFIG["SG_OBJECT_CACHE_SIZE"]) * 1024 * 1024

        # 0 to infinity; higher means objects with smaller sizes are more likely to
        # get evicted than objects that haven't been used for a while.
        # Currently calculated so that an object that hasn't been accessed for 5 minutes has the same
        # removal priority as an object twice its size that's just been accessed.
        self.eviction_decay_constant = float(CONFIG["SG_EVICTION_DECAY"])

        # Objects smaller than this size are assumed to have this size (to simulate the latency of
        # downloading them).
        self.eviction_floor = float(CONFIG["SG_EVICTION_FLOOR"]) * 1024 * 1024

        # Fraction of the cache size to free when eviction is run (the greater value of this amount and the
        # amount needed to download required objects is actually freed). Eviction is an expensive operation
        # (it pauses concurrent downloads) so increasing this makes eviction happen less often at the cost
        # of more possible cache misses.
        self.eviction_min_fraction = float(CONFIG["SG_EVICTION_MIN_FRACTION"])

    def get_downloaded_objects(self, limit_to=None):
        """
        Gets a list of objects currently in the Splitgraph cache (i.e. not only existing externally.)

        :param limit_to: If specified, only the objects in this list will be returned.
        :return: Set of object IDs.
        """
        objects = self.object_engine.run_sql(
            "SELECT splitgraph_api.list_objects()", return_shape=ResultShape.ONE_ONE
        )
        if not limit_to:
            return objects
        else:
            return [o for o in objects if o in limit_to]

    def get_cache_occupancy(self):
        """
        :return: Space occupied by objects cached from external locations, in bytes.
        """
        return int(
            self.object_engine.run_sql(
                SQL("SELECT total_size FROM {}.object_cache_occupancy").format(
                    Identifier(SPLITGRAPH_META_SCHEMA)
                ),
                return_shape=ResultShape.ONE_ONE,
            )
        )

    def _recalculate_cache_occupancy(self):
        """A slower way of getting cache occupancy that actually goes through all objects in the cache status table
        and sums up their size."""
        return int(
            self.object_engine.run_sql(
                SQL(
                    "SELECT COALESCE(sum(splitgraph_api.get_object_size("
                    "quote_ident(t.table_name))), 0)"
                    " FROM information_schema.tables t JOIN {0}.object_cache_status oc"
                    " ON t.table_name = oc.object_id"
                    " WHERE oc.ready = 't' AND t.table_schema = %s"
                ).format(Identifier(SPLITGRAPH_META_SCHEMA)),
                (SPLITGRAPH_META_SCHEMA,),
                return_shape=ResultShape.ONE_ONE,
            )
        )

    def get_total_object_size(self):
        """
        :return: Space occupied by all objects on the engine, in bytes.
        """
        return int(
            self.object_engine.run_sql(
                SQL(
                    "SELECT COALESCE(sum(splitgraph_api.get_object_size("
                    "quote_ident(t.table_name))), 0)"
                    " FROM information_schema.tables t"
                    " WHERE t.table_schema = %s AND t.table_name NOT IN ("
                    + ",".join(itertools.repeat("%s", len(META_TABLES)))
                    + ")"
                ).format(Identifier(SPLITGRAPH_META_SCHEMA)),
                [SPLITGRAPH_META_SCHEMA] + META_TABLES,
                return_shape=ResultShape.ONE_ONE,
            )
        )

    @contextmanager
    def ensure_objects(self, table, objects=None, quals=None, defer_release=False, tracer=None):
        """
        Resolves the objects needed to materialize a given table and makes sure they are in the local
        splitgraph_meta schema.

        Whilst inside this manager, the objects are guaranteed to exist. On exit from it, the objects are marked as
        unneeded and can be garbage collected.

        :param table: Table to materialize
        :param quals: Optional list of qualifiers to be passed to the fragment engine. Fragments that definitely do
            not match these qualifiers will be dropped. See the docstring for `filter_fragments` for the format.
        :param defer_release: If True, won't release the objects on exit.
        :return: If defer_release is True: List of table fragments and a callback that the caller must call
            when the objects are no longer needed. If defer_release is False: just the list of table fragments.
        """

        # Main cache management issue here is concurrency: since we have multiple processes per connection on the
        # server side, if we're accessing this from the FDW, multiple ObjectManagers can fiddle around in the cache
        # status table, triggering weird concurrency cases like:
        #   * We need to download some objects -- another manager wants the same objects. How do we make sure we don't
        #     download them twice? Do we wait on a row-level lock? What happens if another manager crashes?
        #   * We have decided which objects we need -- how do we make sure we don't get evicted by another manager
        #     between us making that decision and increasing the refcount?
        #   * What happens if we crash when we're downloading these objects?

        self.object_engine.run_sql("SET LOCAL synchronous_commit TO OFF")
        tracer = tracer or Tracer()

        if objects is not None:
            required_objects = objects
            logging.info("Using cached objects list")
        else:
            logging.info(
                "Resolving objects for table %s:%s:%s",
                table.repository,
                table.image.image_hash,
                table.table_name,
            )

            # Filter to see if we can discard any objects with the quals
            required_objects = self._filter_objects(table.objects, table, quals)
            tracer.log("filter_objects")

        # Increase the refcount on all of the objects we're giving back to the caller so that others don't GC them.
        logging.info("Claiming %d object(s)", len(required_objects))

        self._claim_objects(required_objects)
        tracer.log("claim_objects")

        try:
            to_fetch = self._prepare_fetch_list(required_objects)
        except SplitGraphError:
            self.object_engine.rollback()
            raise
        tracer.log("prepare_fetch_list")

        # Perform the actual download. If the table has no upstream but still has external locations, we download
        # just the external objects.
        if to_fetch:
            object_locations = self.get_external_object_locations(to_fetch)

            # If all objects are externally hosted, there's no need to try and get the table's
            # upstream (there's a corner case where the metadata engine is different from the object
            # engine and the repo actually has no upstream)
            external_objects = [o[0] for o in object_locations]
            if any(o not in external_objects for o in to_fetch):
                upstream = table.repository.upstream
            else:
                upstream = None

            downloaded_by_us = self.download_objects(
                upstream.objects if upstream else None,
                objects_to_fetch=to_fetch,
                object_locations=object_locations,
            )
            # No matter what, claim the space required by the newly downloaded objects.
            self._increase_cache_occupancy(downloaded_by_us)
            downloaded = self.get_downloaded_objects(limit_to=to_fetch)
            difference = list(set(to_fetch).difference(downloaded))
            if difference:
                error = (
                    "Not all objects required to materialize %s:%s:%s have been fetched. Missing objects: %r"
                    % (
                        table.repository.to_schema(),
                        table.image.image_hash,
                        table.table_name,
                        difference,
                    )
                )
                logging.error(error)
                # Instead of deleting all objects in this batch, discard the cache data
                # on the objects that failed, decrease the refcount on the objects that
                # succeeded and mark them as ready.
                self._delete_cache_entries(difference)
                self._set_ready_flags(downloaded, is_ready=True)
                self._release_objects(downloaded)
                self.object_engine.commit()
                raise ObjectCacheError(error)
            self._set_ready_flags(to_fetch, is_ready=True)
        tracer.log("fetch_objects")
        logging.info("Yielding to the caller")

        release_callback = self._make_release_callback(required_objects, table, tracer)
        try:
            # Release the lock and yield to the caller.
            self.object_engine.commit()
            self.metadata_engine.commit()

            if defer_release:
                yield required_objects, release_callback
            else:
                yield required_objects
        finally:
            if not defer_release:
                release_callback()

    def _make_release_callback(self, required_objects, table, tracer):
        called = False

        def _f(from_fdw=False):
            nonlocal called
            if called:
                return
            called = True
            # Decrease the refcounts on the objects. Optionally, evict them.
            # If it's the caller's responsibility to call this and it crashes,
            # we'll leak memory (hold on to objects in the cache that could have been
            # garbage collected).
            tracer.log("caller")
            self.object_engine.run_sql("SET LOCAL synchronous_commit TO off")
            self._release_objects(required_objects)
            tracer.log("release_objects")
            logging.info("Releasing %d object(s)", len(required_objects))
            logging.info(
                "Timing stats for %s/%s/%s/%s: \n%s",
                table.repository.namespace,
                table.repository.repository,
                table.image.image_hash,
                table.table_name,
                tracer,
            )
            self.object_engine.commit()
            # Release the metadata tables as well
            self.metadata_engine.commit()

        return CallbackList([_f])

    def make_objects_external(self, objects, handler, handler_params):
        """
        Uploads local objects to an external location and marks them as being cached locally (thus making it possible
        to evict or swap them out).

        :param objects: Object IDs to upload. Will do nothing for objects that already exist externally.
        :param handler: Object handler
        :param handler_params: Extra handler parameters
        """
        # Get objects that haven't been uploaded
        uploaded_objects = [o[0] for o in self.get_external_object_locations(objects)]
        new_objects = [o for o in objects if o not in uploaded_objects]

        logging.info(
            "%d object(s) of %d haven't been uploaded yet: %r",
            len(new_objects),
            len(objects),
            new_objects,
        )

        if not new_objects:
            return

        # Similar to claim_objects, make sure we don't deadlock with other uploaders
        # by keeping a consistent order.
        new_objects = sorted(new_objects)

        # Insert the objects into the cache status table (marking them as not ready)
        now = dt.now()
        self.object_engine.run_sql_batch(
            insert("object_cache_status", ("object_id", "ready", "refcount", "last_used"))
            + SQL("ON CONFLICT (object_id) DO UPDATE SET ready = 'f'"),
            [(object_id, False, 1, now) for object_id in new_objects],
        )

        # Grab the objects that we're supposed to be uploading.
        claimed_objects = self.object_engine.run_sql(
            select("object_cache_status", "object_id", "ready = 'f' FOR UPDATE"),
            return_shape=ResultShape.MANY_ONE,
        )
        new_objects = [o for o in new_objects if o in claimed_objects]

        # Perform the actual upload
        external_handler = get_external_object_handler(handler, handler_params)
        with switch_engine(self.object_engine):
            uploaded = external_handler.upload_objects(new_objects, self.metadata_engine)
        locations = [(oid, url, handler) for oid, url in zip(new_objects, uploaded)]
        self.register_object_locations(locations)

        # Increase the cache occupancy since the objects can now be evicted.
        self._increase_cache_occupancy(new_objects)

        # Mark the objects as ready and decrease their refcounts.
        self._set_ready_flags(new_objects, True)
        self._release_objects(new_objects)

        # Perform eviction in case we've reached the capacity of the cache
        excess = self.get_cache_occupancy() - self.cache_size
        if excess > 0:
            self.run_eviction(keep_objects=[], required_space=excess)

    def _filter_objects(self, objects, table, quals):
        if quals:
            column_types = {c[1]: c[2] for c in table.table_schema}
            filtered_objects = self.filter_fragments(objects, quals, column_types)
            logging.info(
                "Qual filter: discarded %d/%d object(s)",
                len(objects) - len(filtered_objects),
                len(objects),
            )
            # Make sure to keep the order
            objects = [r for r in objects if r in filtered_objects]
        return objects

    def _prepare_fetch_list(self, required_objects):
        """
        Calculates the missing objects and ensures there's enough space in the cache
        to download them.

        :param required_objects: Iterable of object IDs that are required to be on the engine.
        :return: Set of objects to fetch
        """
        to_fetch = self.object_engine.run_sql(
            select("object_cache_status", "object_id", "ready = 'f'"),
            return_shape=ResultShape.MANY_ONE,
        )
        if to_fetch:
            # If we need to download anything, take out an exclusive lock on the cache since we might
            # need to run eviction and don't want multiple managers trying to download the same things.
            # This used to be more granular (allow multiple managers downloading objects) but was resulting
            # in fun concurrency bugs and deadlocks that I don't have the willpower to investigate further
            # right now (e.g. two managers trying to download the same objects at the same time after one of them
            # runs cache eviction and releases some locks -- or two managers trying to free the same amount
            # of space in the cache for the same set of objects).

            # Since we already hold a row-level lock on some objects in the cache, we have to release it first and
            # lock the full table then -- but this means someone else might start downloading the objects that
            # we claimed. So, once we acquire the lock, we recalculate the fetch list again to see what
            # we're supposed to be fetching.
            self.object_engine.commit()
            self.object_engine.lock_table(SPLITGRAPH_META_SCHEMA, "object_cache_status")
            to_fetch = self.object_engine.run_sql(
                select("object_cache_status", "object_id", "ready = 'f'"),
                return_shape=ResultShape.MANY_ONE,
            )
            # If someone else downloaded all the objects we need, there's no point in holding the lock.
            # This is tricky to test with a single process.
            if not to_fetch:  # pragma: no cover
                self.object_engine.commit()
                return to_fetch
            required_space = sum(o.size for o in self.get_object_meta(list(to_fetch)).values())
            current_occupied = self.get_cache_occupancy()
            logging.info(
                "Need to download %d object(s) (%s), cache occupancy: %s/%s",
                len(to_fetch),
                pretty_size(required_space),
                pretty_size(current_occupied),
                pretty_size(self.cache_size),
            )
            # If the total cache size isn't large enough, there's nothing we can do without cooperating with the
            # caller and seeing if they can use the objects one-by-one.
            if required_space > self.cache_size:
                raise ObjectCacheError(
                    "Not enough space in the cache to download the required objects!"
                )
            if required_space > self.cache_size - current_occupied:
                to_free = required_space + current_occupied - self.cache_size
                logging.info("Need to free %s", pretty_size(to_free))
                self.run_eviction(required_objects, to_free)
            self.object_engine.commit()
            # Finally, after we're done with eviction, relock the objects that we're supposed to be downloading.
            to_fetch = self.object_engine.run_sql(
                select("object_cache_status", "object_id", "ready = 'f' FOR UPDATE"),
                return_shape=ResultShape.MANY_ONE,
            )
        return to_fetch

    def _claim_objects(self, objects):
        """Increases refcounts and bumps the last used timestamp to now for cached objects.
        For objects that aren't in the cache, checks that they don't already exist locally and then
        adds them to the cache status table, marking them with ready=False
        (which must be set to True by the end of the operation)."""
        if not objects:
            return
        now = dt.utcnow()
        # Objects that were created locally aren't supposed to be claimed here or have an entry in the cache.
        # So, we first try to update cache entries to bump their refcount, see which ones we updated,
        # subtract objects that we have locally and insert the remaining entries as new cache entries.

        claimed = self.object_engine.run_sql(
            SQL(
                "UPDATE {}.object_cache_status SET refcount = refcount + 1, "
                "last_used = %s WHERE object_id IN ("
            ).format(Identifier(SPLITGRAPH_META_SCHEMA))
            + SQL(",".join(itertools.repeat("%s", len(objects))))
            + SQL(") RETURNING object_id"),
            [now] + objects,
            return_shape=ResultShape.MANY_ONE,
        )
        claimed = claimed or []
        remaining = set(objects).difference(set(claimed))
        remaining = remaining.difference(set(self.get_downloaded_objects(limit_to=list(remaining))))

        # Since we send multiple queries, each claiming a single remaining object, we can deadlock here
        # with another object manager instance. Hence, we sort the list of objects so that we claim them
        # in a consistent order between all instances.
        remaining = sorted(remaining)

        # Remaining: objects that are new to the cache and that we'll need to download. However, between us
        # running the first query and now, somebody else might have started downloading them. Hence, when
        # we try to insert them, we'll be blocked until the other engine finishes its download and commits
        # the transaction -- then get an integrity error. So here, we do an update on conflict (again).
        self.object_engine.run_sql_batch(
            insert("object_cache_status", ("object_id", "ready", "refcount", "last_used"))
            + SQL(
                "ON CONFLICT (object_id) DO UPDATE SET refcount = EXCLUDED.refcount + 1, last_used = %s"
            ),
            [(object_id, False, 1, now, now) for object_id in remaining],
        )

    def _set_ready_flags(self, objects, is_ready=True):
        if objects:
            self.object_engine.run_sql(
                SQL(
                    "UPDATE {0}.object_cache_status SET ready = %s WHERE object_id IN ("
                    + ",".join(itertools.repeat("%s", len(objects)))
                    + ")"
                ).format(Identifier(SPLITGRAPH_META_SCHEMA)),
                [is_ready] + list(objects),
            )

    def _release_objects(self, objects):
        """Decreases objects' refcounts."""
        if objects:
            self.object_engine.run_sql(
                SQL(
                    "UPDATE {}.{} SET refcount = refcount - 1 WHERE object_id IN ("
                    + ",".join(itertools.repeat("%s", len(objects)))
                    + ")"
                ).format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier("object_cache_status")),
                objects,
            )

    def _increase_cache_occupancy(self, objects):
        """Increase the cache occupancy by objects' total size."""
        if not objects:
            return
        total_size = sum(o.size for o in self.get_object_meta(objects).values())
        self.object_engine.run_sql(
            SQL("UPDATE {}.object_cache_occupancy SET total_size = total_size + %s").format(
                Identifier(SPLITGRAPH_META_SCHEMA)
            ),
            (total_size,),
        )

    def _decrease_cache_occupancy(self, size_freed):
        """Decrease the cache occupancy by a given size."""
        self.object_engine.run_sql(
            SQL("UPDATE {}.object_cache_occupancy SET total_size = total_size - %s").format(
                Identifier(SPLITGRAPH_META_SCHEMA)
            ),
            (size_freed,),
        )

    def run_eviction(self, keep_objects, required_space=None):
        """
        Delete enough objects with zero reference count (only those, since we guarantee that whilst refcount is >0,
        the object stays alive) to free at least `required_space` in the cache.

        :param keep_objects: List of objects (besides those with nonzero refcount) that can't be deleted.
        :param required_space: Space, in bytes, to free. If the routine can't free at least this much space,
            it shall raise an exception. If None, removes all eligible objects.
        """

        now = dt.utcnow()

        def _eviction_score(object_size, last_used):
            # We want to evict objects in order to minimize
            # P(object is requested again) * (cost of redownloading the object).
            # To approximate the probability, we use an exponential decay function (1 if last_used = now, dropping down
            # to 0 as time since the object's last usage time passes).
            # To approximate the cost, we use the object's size, floored to a constant (so if the object has
            # size <= floor, we'd use the floor value -- this is to simulate the latency of re-fetching the object,
            # as opposed to the bandwidth)
            time_since_used = (now - last_used).total_seconds()
            time_factor = math.exp(-self.eviction_decay_constant * time_since_used)
            size_factor = object_size if object_size > self.eviction_floor else self.eviction_floor
            return time_factor * size_factor

        logging.info("Performing eviction...")
        # Maybe here we should also do the old cleanup (see if the objects aren't required
        #   by any of the current repositories at all).

        # Find deletion candidates: objects that we have locally, with refcount 0, that aren't in the whitelist.

        candidates = [
            o
            for o in self.object_engine.run_sql(
                select("object_cache_status", "object_id,last_used", "refcount=0"),
                return_shape=ResultShape.MANY_MANY,
            )
            if o[0] not in keep_objects
        ]

        object_meta = self.get_object_meta([o[0] for o in candidates]) if candidates else {}
        object_sizes = {o.object_id: o.size for o in object_meta.values()}

        # Also delete objects that don't have a metadata entry at all
        orphaned_objects = [o[0] for o in candidates if o[0] not in object_sizes]
        orphaned_object_sizes = {o: self.object_engine.get_object_size(o) for o in orphaned_objects}
        if orphaned_objects:
            logging.info(
                "Found %d orphaned object(s), total size %s: %s",
                len(orphaned_objects),
                pretty_size(sum(orphaned_object_sizes.values())),
                orphaned_objects,
            )

        if required_space is None:
            # Just delete everything with refcount 0.
            to_delete = [o[0] for o in candidates]
            freed_space = sum(object_sizes.values()) + sum(orphaned_object_sizes.values())
            logging.info(
                "Will delete %d object(s), total size %s", len(to_delete), pretty_size(freed_space)
            )
        else:
            if required_space > sum(object_sizes.values()) + sum(orphaned_object_sizes.values()):
                raise ObjectCacheError("Not enough space will be reclaimed after eviction!")

            # Since we can free the minimum required amount of space, see if we can free even more as
            # per our settings (if we can't, we'll just delete as much as we can instead of failing).
            required_space = max(required_space, int(self.eviction_min_fraction * self.cache_size))

            # Delete all orphaned objects first
            to_delete = orphaned_objects
            last_useds = [o[1] for o in candidates if o[0] in orphaned_objects]
            freed_space = sum(orphaned_object_sizes.values())

            # Sort candidates by deletion priority (lowest is smallest expected retrieval cost -- more likely to delete)
            candidates = sorted(
                [o for o in candidates if o[0] not in orphaned_objects],
                key=lambda o: _eviction_score(object_sizes[o[0]], o[1]),
            )

            # Keep adding deletion candidates until we've freed enough space.
            for object_id, last_used in candidates:
                if freed_space >= required_space:
                    break
                last_useds.append(last_used)
                to_delete.append(object_id)
                freed_space += object_sizes[object_id]
            logging.info(
                "Will delete %d object(s) last used between %s and %s, total size %s: %s",
                len(to_delete),
                min(last_useds).isoformat(),
                max(last_useds).isoformat(),
                pretty_size(freed_space),
                to_delete,
            )

        if to_delete:
            # NB delete_objects commits as well, releasing the lock. Make sure to do all bookkeeping first so that
            # other object managers in this function think that the objects have been deleted and don't try to delete
            # them again.
            self._delete_cache_entries(to_delete)
            self._decrease_cache_occupancy(freed_space)
            self.delete_objects(to_delete)
            logging.info(
                "Eviction done. Cache occupancy: %s", pretty_size(self.get_cache_occupancy())
            )

    def _delete_cache_entries(self, to_delete):
        self.object_engine.run_sql(
            SQL(
                "DELETE FROM {}.{} WHERE object_id IN ("
                + ",".join(itertools.repeat("%s", len(to_delete)))
                + ")"
            ).format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier("object_cache_status")),
            to_delete,
        )

    def download_objects(self, source, objects_to_fetch, object_locations):
        """
        Fetches the required objects from the remote and stores them locally.
        Does nothing for objects that already exist.

        :param source: Remote ObjectManager. If None, will only try to download objects from the external location.
        :param objects_to_fetch: List of object IDs to download.
        :param object_locations: List of custom object locations, encoded as tuples (object_id, object_url, protocol).
        """

        existing_objects = self.get_downloaded_objects(limit_to=objects_to_fetch)
        logging.info("Need to fetch %r, already exist: %r", objects_to_fetch, existing_objects)
        objects_to_fetch = list(set(o for o in objects_to_fetch if o not in existing_objects))
        if not objects_to_fetch:
            return []

        total_size = sum(o.size for o in self.get_object_meta(objects_to_fetch).values())
        logging.info(
            "Fetching %d object(s), total size %s", len(objects_to_fetch), pretty_size(total_size)
        )

        # We don't actually seem to pass extra handler parameters when downloading objects since
        # we can have multiple handlers in this batch.
        external_objects = _fetch_external_objects(
            self.object_engine,
            source.metadata_engine if source else self.metadata_engine,
            object_locations,
            {},
        )

        remaining_objects_to_fetch = [o for o in objects_to_fetch if o not in external_objects]
        if not remaining_objects_to_fetch or not source:
            return external_objects

        remote_objects = self.object_engine.download_objects(
            remaining_objects_to_fetch, source.object_engine
        )
        return external_objects + remote_objects

    def upload_objects(self, target, objects_to_push, handler="DB", handler_params=None):
        """
        Uploads physical objects to the remote or some other external location.

        :param target: Target ObjectManager
        :param objects_to_push: List of object IDs to upload.
        :param handler: Name of the handler to use to upload objects. Use `DB` to push them to the remote, `FILE`
            to store them in a directory that can be accessed from the client and `HTTP` to upload them to HTTP.
        :param handler_params: For `HTTP`, a dictionary `{"username": username, "password", password}`. For `FILE`,
            a dictionary `{"path": path}` specifying the directory where the objects shall be saved.
        :return: A list of (object_id, url, handler) that specifies all objects were uploaded (skipping objects that
            already exist on the remote).
        """
        if handler_params is None:
            handler_params = {}

        # Check which objects we need to push out
        objects_to_push = target.get_new_objects(objects_to_push)
        if not objects_to_push:
            logging.info("Nothing to upload.")
            return []
        total_size = sum(o.size for o in self.get_object_meta(objects_to_push).values())
        logging.info(
            "Uploading %d object(s), total size %s", len(objects_to_push), pretty_size(total_size)
        )

        if handler == "DB":
            self.object_engine.upload_objects(objects_to_push, target.object_engine)
            # We assume that if the object doesn't have an explicit location, it lives on the remote.
            return []

        external_handler = get_external_object_handler(handler, handler_params)
        with switch_engine(self.object_engine):
            uploaded = external_handler.upload_objects(objects_to_push, target.metadata_engine)
        return [(oid, url, handler) for oid, url in zip(objects_to_push, uploaded)]

    def cleanup(self, include_physical_objects=True):
        """
        Deletes all objects in the object_tree not required by any current repository, including their dependencies and
        their remote locations. Also deletes all objects not registered in the object_tree.

        :param include_physical_objects: Default True. If False, only deletes the object metadata rather
            than any physical objects on the engine.
        """
        # First, get a list of all objects required by a table.
        table_objects = {
            o
            for os in self.metadata_engine.run_sql(
                SQL("SELECT object_ids FROM {}.tables").format(Identifier(SPLITGRAPH_META_SCHEMA)),
                return_shape=ResultShape.MANY_ONE,
            )
            for o in os
        }

        # Go through the tables that aren't repository-dependent and delete entries there.
        tables = ["object_locations", "objects"]
        if include_physical_objects:
            tables.append("object_cache_status")
        for table_name in ["object_locations", "object_cache_status", "objects"]:
            query = SQL("DELETE FROM {}.{}").format(
                Identifier(SPLITGRAPH_META_SCHEMA), Identifier(table_name)
            )
            if table_objects:
                query += SQL(
                    " WHERE object_id NOT IN ("
                    + ",".join("%s" for _ in range(len(table_objects)))
                    + ")"
                )
            if table_name == "object_cache_status":
                self.object_engine.run_sql(query, list(table_objects))
            else:
                self.metadata_engine.run_sql(query, list(table_objects))

        if include_physical_objects:
            # Go through the physical objects and delete them as well
            # This is slightly dirty, but since the info about the objects was deleted on rm, we just say that
            # anything in splitgraph_meta that's not a system table is fair game.
            tables_in_meta = {
                c
                for c in self.object_engine.get_all_tables(SPLITGRAPH_META_SCHEMA)
                if c not in META_TABLES
            }
            tables_in_meta.update(self.get_downloaded_objects())

            to_delete = tables_in_meta.difference(table_objects)
            self.delete_objects(to_delete)

            # Recalculate the object cache occupancy
            self.object_engine.run_sql(
                SQL("UPDATE {}.object_cache_occupancy SET total_size = %s").format(
                    Identifier(SPLITGRAPH_META_SCHEMA)
                ),
                (self._recalculate_cache_occupancy(),),
            )
            return to_delete

    def delete_objects(self, objects):
        """
        Deletes objects from the Splitgraph cache

        :param objects: A sequence of objects to be deleted
        """
        objects = list(objects)
        for i in range(0, len(objects), 100):
            to_delete = objects[i : i + 100]
            table_types = self.object_engine.run_sql(
                SQL(
                    "SELECT table_name, table_type FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name IN ("
                    + ",".join(itertools.repeat("%s", len(to_delete)))
                    + ")"
                ),
                [SPLITGRAPH_META_SCHEMA] + to_delete,
            )

            base_tables = [tn for tn, tt in table_types if tt == "BASE TABLE"]
            # Try deleting CStore-mounted objects regardless of whether they're
            # in splitgraph_meta as foreign tables (there might be cases
            # where they are in /var/lib/splitgraph/objects but not mounted)
            foreign_tables = [tn for tn in to_delete if tn not in base_tables]

            if base_tables:
                self.object_engine.run_sql(
                    SQL(";").join(
                        SQL("DROP TABLE {}.{}").format(
                            Identifier(SPLITGRAPH_META_SCHEMA), Identifier(t)
                        )
                        for t in base_tables
                    )
                )
            if foreign_tables:
                self.object_engine.delete_objects(foreign_tables)

            self.object_engine.commit()


def _fetch_external_objects(engine, source_engine, object_locations, handler_params):
    non_remote_objects = []
    non_remote_by_method = defaultdict(list)
    for object_id, object_url, protocol in object_locations:
        non_remote_by_method[protocol].append((object_id, object_url))
        non_remote_objects.append(object_id)
    if non_remote_objects:
        logging.info("Fetching external objects...")
        for method, objects in non_remote_by_method.items():
            handler = get_external_object_handler(method, handler_params)
            # In case we're calling this from inside the FDW
            with switch_engine(engine):
                handler.download_objects(objects, source_engine)
    return non_remote_objects
