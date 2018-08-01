from flask_login import UserMixin
from sqlalchemy.dialects import postgresql as pg
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect
from datetime import datetime
import dateutil
import stravalib
import polyline
import pymongo
import itertools

from redis import Redis

import pandas as pd
import gevent
from gevent.queue import Queue
from gevent.pool import Pool
from exceptions import StopIteration
import requests
# from requests.exceptions import HTTPError
import cPickle
import msgpack
from bson import ObjectId
from bson.binary import Binary
from heatflask import app

import os

CONCURRENCY = app.config["CONCURRENCY"]
STREAMS_OUT = ["polyline", "time"]
STREAMS_TO_CACHE = ["polyline", "time"]
CACHE_USERS_TIMEOUT = app.config["CACHE_USERS_TIMEOUT"]
CACHE_ACTIVITIES_TIMEOUT = app.config["CACHE_ACTIVITIES_TIMEOUT"]
INDEX_UPDATE_TIMEOUT = app.config["INDEX_UPDATE_TIMEOUT"]
LOCAL = os.environ.get("APP_SETTINGS") == "config.DevelopmentConfig"
OFFLINE = app.config.get("OFFLINE")

# PostgreSQL access via SQLAlchemy
db_sql = SQLAlchemy(app)  # , session_options={'expire_on_commit': False})
Column = db_sql.Column
String, Integer, Boolean = db_sql.String, db_sql.Integer, db_sql.Boolean

# MongoDB access via PyMongo
mongo_client = pymongo.MongoClient(app.config.get("MONGODB_URI"))
mongodb = mongo_client.get_database()

# Redis data-store
redis = Redis.from_url(app.config["REDIS_URL"])


class Users(UserMixin, db_sql.Model):
    id = Column(Integer, primary_key=True, autoincrement=False)

    # These fields get refreshed every time the user logs in.
    #  They are only stored in the database to enable persistent login
    username = Column(String())
    firstname = Column(String())
    lastname = Column(String())
    profile = Column(String())
    access_token = Column(String())

    measurement_preference = Column(String())
    city = Column(String())
    state = Column(String())
    country = Column(String())
    email = Column(String())

    dt_last_active = Column(pg.TIMESTAMP)
    app_activity_count = Column(Integer, default=0)
    share_profile = Column(Boolean, default=False)

    activity_index = None
    index_df_dtypes = {
        "id": "uint32",
        "group": "uint16",
        "type": "category",
        "total_distance": "float32",
        "elapsed_time": "uint32",
        "average_speed": "float16"
    }

    index_df_out_dtypes = {
        "type": str,
        "total_distance": float,
        "elapsed_time": int,
        "average_speed": float,
        "ts_local": str,
        "ts_UTC": str,
        "group": int
    }

    def db_state(self):
        state = inspect(self)
        attrs = ["transient", "pending", "persistent", "deleted", "detached"]
        return [attr for attr in attrs if getattr(state, attr)]

    def serialize(self):
        return cPickle.dumps(self)

    def info(self):
        profile = {}
        profile.update(vars(self))
        del profile["_sa_instance_state"]
        if "activity_index" in profile:
            del profile["activity_index"]
        # app.logger.debug("{}: {}".format(self, profile))
        return profile

    @classmethod
    def from_serialized(cls, p):
        return cPickle.loads(p)

    def client(self):
        return stravalib.Client(
            access_token=self.access_token,
            rate_limiter = lambda x=None: None
            #rate_limit_requests=False
        )

    def __repr__(self):
        return "<User %r>" % (self.id)

    def get_id(self):
        return unicode(self.id)

    def is_admin(self):
        return self.id in app.config["ADMIN"]

    @staticmethod
    def strava_data_from_token(token):
        client = stravalib.Client(access_token=token)
        try:
            strava_user = client.get_athlete()
        except Exception as e:
            app.logger.error("error getting user data from token: {}"
                             .format(e))
        else:
            return {
                "id": strava_user.id,
                "username": strava_user.username,
                "firstname": strava_user.firstname,
                "lastname": strava_user.lastname,
                "profile": strava_user.profile_medium or strava_user.profile,
                "measurement_preference": strava_user.measurement_preference,
                "city": strava_user.city,
                "state": strava_user.state,
                "country": strava_user.country,
                "email": strava_user.email,
                "access_token": token
            }

    @staticmethod
    def key(identifier):
        return "U:{}".format(identifier)

    def cache(self, identifier=None, timeout=CACHE_USERS_TIMEOUT):
        key = self.__class__.key(identifier or self.id)
        packed = self.serialize()
        # app.logger.debug(
        #     "caching {} with key '{}' for {} sec. size={}"
        #     .format(self, key, timeout, len(packed))
        # )
        return redis.setex(key, packed, timeout)

    def uncache(self):
        # app.logger.debug("uncaching {}".format(self))

        # delete from cache too.  It may be under two different keys
        redis.delete(self.__class__.key(self.id))
        redis.delete(self.__class__.key(self.username))

    def update_usage(self):
        self.dt_last_active = datetime.utcnow()
        self.app_activity_count = self.app_activity_count + 1
        db_sql.session.commit()
        self.cache()
        return self

    @classmethod
    def add_or_update(cls, cache_timeout=CACHE_USERS_TIMEOUT, **kwargs):
        if not kwargs:
            app.logger.debug("attempted to add_or_update user with no data")
            return

        # Creates a new user or updates an existing user (with the same id)
        detached_user = cls(**kwargs)
        try:
            persistent_user = db_sql.session.merge(detached_user)
            db_sql.session.commit()

        except Exception as e:
            db_sql.session.rollback()
            app.logger.error(
                "error adding/updating user {}: {}".format(kwargs, e))
        else:
            if persistent_user:
                persistent_user.cache(cache_timeout)
                # app.logger.info("updated {} with {}"
                #                 .format(persistent_user, kwargs))
            return persistent_user

    @classmethod
    def get(cls, user_identifier, timeout=CACHE_USERS_TIMEOUT):
        key = cls.key(user_identifier)
        cached = redis.get(key)
        if cached:
            redis.expire(key, CACHE_USERS_TIMEOUT)
            try:
                user = cls.from_serialized(cached)
                # app.logger.debug(
                #     "retrieved {} from cache with key {}".format(user, key))
                return db_sql.session.merge(user, load=False)
            except Exception:
                # apparently this cached user object is no good so let's
                #  delete it
                redis.delete(key)

        # Get user from db by id or username
        try:
            # try casting identifier to int
            user_id = int(user_identifier)
        except ValueError:
            # if that doesn't work then assume it's a string username
            user = cls.query.filter_by(username=user_identifier).first()
        else:
            user = cls.query.get(user_id)

        if user:
            user.cache(user_identifier, timeout)

        return user if user else None

    def delete(self):
        self.delete_index()
        self.uncache()
        try:
            self.client().deauthorize()
        except Exception:
            pass
        db_sql.session.delete(self)
        db_sql.session.commit()

    @classmethod
    def update_all(cls, delete=False):
        with app.app_context():
            def user_data(user):
                data = cls.strava_data_from_token(user.access_token)
                # check = "valid" if user else "INVALID"
                # app.logger.info(
                #     "token for {} is {}".format(user, check)
                # )
                return data if data else user

            P = Pool()
            num_deleted = 0
            count = 0
            try:
                for obj in P.imap_unordered(user_data, cls.query):
                    if type(obj) == cls:
                        msg = "invalid access token for {}".format(obj)
                        if delete:
                            obj.delete()
                            msg += "...deleted"
                            num_deleted += 1
                    else:
                        user = cls.add_or_update(cache_timeout=60, **obj)
                        msg = "successfully updated {}".format(user)
                    # app.logger.info(msg)
                    yield msg + "\n"
                    count += 1

                EventLogger.new_event(
                    msg="updated Users database: deleted {} invalid users, count={}"
                        .format(num_deleted, count - num_deleted)
                )

                yield (
                    "done! {} invalid users deleted, old count: {}, new count: {}"
                    .format(num_deleted, count, count - num_deleted)
                )
            except Exception as e:
                app.logger.info("error: {}".format(e))
                P.kill()

    @classmethod
    def dump(cls, attrs, **filter_by):
        dump = [{attr: getattr(user, attr) for attr in attrs}
                for user in cls.query.filter_by(**filter_by)]
        return dump

    @classmethod
    def backup(cls):
        fields = [
            "id", "access_token", "dt_last_active", "app_activity_count",
            "share_profile"
        ]
        dump = cls.dump(fields)

        mongodb.users.insert_one({"backup": dump, "ts": datetime.utcnow()})
        return dump

    @classmethod
    def restore(cls, users_list=None):

        def update_user_data(user_data):
            strava_data = cls.strava_data_from_token(
                user_data.get("access_token")
            )
            if strava_data:
                user_data.update(strava_data)
                return user_data
            else:
                app.logger.info("problem updating user {}"
                                .format(user_data["id"]))

        if not users_list:
            doc = mongodb.users.find_one()
            if doc:
                users_list = doc.get("backup")
            else:
                return

        # erase user table
        result = db_sql.drop_all()
        app.logger.info("dropping Users table: {}".format(result))

        # delete all users from the Redis cache
        keys_to_delete = redis.keys(Users.key("*"))
        if keys_to_delete:
            result = redis.delete(*keys_to_delete)
            app.logger.info("dropping cached User objects: {}".format(result))

        # create new user table
        result = db_sql.create_all()
        app.logger.info("creating Users table: {}".format(result))

        # rebuild table with user backup updated with current info from Strava
        count_before = len(users_list)
        count = 0
        P = Pool(CONCURRENCY)
        for user_dict in P.imap_unordered(update_user_data, users_list):
            if user_dict:
                user = cls.add_or_update(cache_timeout=10, **user_dict)
                if user:
                    count += 1
                    app.logger.debug("successfully restored/updated {}"
                                     .format(user))
        return {
            "operation": "users restore",
            "before": count_before,
            "after": count
        }

    def delete_index(self):
        try:
            result1 = mongodb.indexes.delete_one({'_id': self.id})
        except Exception as e:
            app.logger.error("error deleting index {} from MongoDB:\n{}"
                             .format(self, e))
            result1 = e

        self.activity_index = None
        result2 = self.cache()
        #app.logger.debug("delete index for {}. mongo:{}, redis:{}".format(self.id, vars(result1), result2))

        return result1, result2

    def indexing(self, status=None):
        # Indicate to other processes that we are currently indexing
        #  This should not take any longer than 30 seconds
        key = "indexing {}".format(self.id)
        if status is None:
            return redis.get(key) == "True"
        elif status:
            return redis.setex(key, status, 60)
        else:
            redis.delete(key)

    def get_index(self):
        if not self.activity_index:
            try:
                self.activity_index = (mongodb.indexes
                                       .find_one({"_id": self.id}))
            except Exception as e:
                app.logger.error(
                    "error accessing mongodb indexes collection:\n{}"
                    .format(e))

        if self.activity_index:
            return {
                "index_df": pd.read_msgpack(
                    self.activity_index["packed_index"]
                ).astype({"type": str}),
                "dt_last_indexed": self.activity_index["dt_last_indexed"]
            }

    @staticmethod
    def to_datetime(obj):
        if not obj:
            return
        if isinstance(obj, datetime):
            return obj
        try:
            dt = dateutil.parser.parse(obj)
        except ValueError:
            return
        else:
            return dt

    def build_index(self, out_queue=None,
                    limit=None,
                    after=None, before=None,  # datetime object or string
                    activity_ids=None):

        def enqueue(msg):
            if out_queue is None:
                pass
            else:
                # app.logger.debug(msg)
                out_queue.put(msg)

        before = self.__class__.to_datetime(before)
        after = self.__class__.to_datetime(after)

        def in_date_range(dt):
            if not (before or after):
                return

            t1 = (not after) or (after <= dt)
            t2 = (not before) or (dt <= before)
            result = (t1 and t2)
            # app.logger.info("{} <= {} <= {}: {}"
            #                 .format(after, dt, before, result))
            return result

        self.indexing(True)
        start_time = datetime.utcnow()
        app.logger.debug("building activity index for {}".format(self.id))

        activities_list = []
        count = 0
        rendering = True
        try:
            for a in self.client().get_activities():
                d = Activities.strava2dict(a)
                if d.get("summary_polyline"):
                    activities_list.append(d)
                    count += 1

                    if (rendering and
                        ((activity_ids and (d["id"] in activity_ids)) or
                         (limit and (count <= limit)) or
                            in_date_range(d["ts_local"]))):

                        d2 = dict(d)
                        d2["ts_local"] = str(d2["ts_local"])
                        enqueue(d2)

                        if activity_ids:
                            activity_ids.remove(d["id"])
                            if not activity_ids:
                                rendering = False
                                enqueue({"stop_rendering": "1"})

                    if (rendering and
                        ((limit and count >= limit) or
                            (after and (d["ts_local"] < after)))):
                        rendering = False
                        enqueue({"stop_rendering": "1"})

                    enqueue({"msg": "indexing...{} activities"
                             .format(count)})
                    gevent.sleep(0)

            gevent.sleep(0)
        except Exception as e:
            enqueue({"error": str(e)})
            app.logger.debug("Error while building activity index")
            app.logger.error(e)
        else:
            # If we are streaming to a client, this is where we tell it
            #  stop listening by pushing a StopIteration the queue
            if not activities_list:
                enqueue({"error": "No activities!"})
                enqueue(StopIteration)
                self.indexing(False)
                EventLogger.new_event(msg="no activities for {}"
                                      .format(self.id))
                return

            index_df = (pd.DataFrame(activities_list)
                        .astype(Users.index_df_dtypes)
                        .sort_values(by="id", ascending=False))

            # app.logger.info(index_df.info())

            packed = Binary(index_df.to_msgpack(compress='blosc'))
            self.activity_index = {
                "dt_last_indexed": datetime.utcnow(),
                "packed_index": packed
            }

            # update the cache for this user
            self.cache()

            # if MongoDB access fails then at least the activity index
            # is cached with the user for a while.  Since we're using
            # a cheap sandbox version of Mongo, it might be down sometimes.
            try:
                result = mongodb.indexes.update_one(
                    {"_id": self.id},
                    {"$set": self.activity_index},
                    upsert=True)

                # app.logger.info(
                #     "inserted activity index for {} in MongoDB: {}"
                #     .format(self, vars(result))
                # )
            except Exception as e:
                app.logger.error(
                    "error wrtiting activity index for {} to MongoDB:\n{}"
                    .format(self, e)
                )

            elapsed = datetime.utcnow() - start_time
            msg = (
                "{}'s index built in {} sec. count={}, size={}"
                .format(self.id,
                        round(elapsed.total_seconds(), 3),
                        count,
                        len(packed))
            )

            app.logger.debug(msg)
            EventLogger.new_event(msg=msg)
            enqueue({"msg": "done indexing {} activities.".format(count)})

        finally:
            self.indexing(False)
            enqueue(StopIteration)

        if activities_list:
            return index_df

    def update_index(self, index_df=None, activity_ids=[], reset_ttl=True):
        start_time = datetime.utcnow()

        #  retrieve the current index if we have it, otherwise return nothing
        if index_df is None:
            activity_index = self.get_index()
            if activity_index:
                index_df = activity_index["index_df"]
            else:
                return

        index_df = index_df.set_index("id")

        activities_list = []
        app.logger.info("Updating activity index for {}".format(self))

        if activity_ids:
            for aid in activity_ids:
                try:
                    act = Activities.strava2dict(
                        self.client().get_activity(aid)
                    )
                except Exception as e:
                    app.logger.error("error getting {}'s activity {}: {}"
                                     .format(self, aid, e))
                else:
                    activities_list.append(act)
        else:
            # If ids of activities to update are not explicitly given
            #   (most often the case), we will replace the 10 most recent
            #  activities, since these are the ones most likely to have been
            #  manually modified since last access.
            try:
                activities_list = [
                    Activities.strava2dict(a)
                    for a in self.client().get_activities(limit=10)
                ]
            except Exception as e:
                app.logger.error(
                    "was not able to retrieve {}'s latest activity data: {}"
                    .format(self, e)
                )
                return index_df

        to_update = {}
        if reset_ttl:
            # update the timestamp
            to_update["dt_last_indexed"] = datetime.utcnow()

        if activities_list:
            # new_df = (
            #     pd.DataFrame(activities_list)
            #     .astype(Users.index_df_dtypes)
            #     .set_index("id")
            # )

            index_df = (
                pd.DataFrame(activities_list)
                .astype(Users.index_df_dtypes)
                .set_index("id")
                .combine_first(index_df)
                .sort_index(ascending=False)
                .reset_index()
            )

            # app.logger.info("after update: {}".format(index_df.info()))

            to_update["packed_index"] = (
                Binary(index_df.to_msgpack(compress='blosc'))
            )

        if to_update:
            # update activity_index in this user
            self.activity_index.update(to_update)

            # update the cache entry for this user (necessary?)
            self.cache()

            # update activity_index in MongoDB
            try:
                mongodb.indexes.update_one(
                    {"_id": self.id},
                    {"$set": to_update}
                )
            except Exception as e:
                app.logger.debug(
                    "error updating activity index for {} in MongoDB:\n{}"
                    .format(self, e)
                )

        elapsed = datetime.utcnow() - start_time
        num = len(activities_list)
        msg = (
            "updated {} index activit{} for user {} in {} sec."
            .format(num,
                    "y" if num == 1 else "ies",
                    self.id,
                    round(elapsed.total_seconds(), 3))
        )

        app.logger.info(msg)
        if to_update:
            return index_df

    def query_activities(self, activity_ids=None,
                         limit=None,
                         after=None, before=None,
                         only_ids=False,
                         summaries=True,
                         streams=False,
                         owner_id=False,
                         build_index=True,
                         pool=None,
                         out_queue=None,
                         cache_timeout=CACHE_ACTIVITIES_TIMEOUT,
                         **kwargs):

        if self.indexing():
            return [{
                    "error": "Building activity index for {}".format(self.id)
                    + "...<br>Please try again in a few seconds.<br>"
                    }]

        # convert date strings to datetimes, if applicable
        if before or after:
            try:
                after = self.__class__.to_datetime(after)
                if before:
                    before = self.__class__.to_datetime(before)
                    assert(before > after)
            except AssertionError:
                return [{"error": "Invalid Dates"}]

        # app.logger.info("query_activities called with: {}".format({
        #     "activity_ids": activity_ids,
        #     "limit": limit,
        #     "after": after,
        #     "before": before,
        #     "only_ids": only_ids,
        #     "summaries": summaries,
        #     "streams": streams,
        #     "owner_id": owner_id,
        #     "build_index": build_index,
        #     "pool": pool,
        #     "out_queue": out_queue
        # }))

        def import_streams(client, queue, activity):
            # app.logger.debug("importing {}".format(activity["id"]))

            stream_data = Activities.import_streams(
                client, activity["id"], STREAMS_TO_CACHE, cache_timeout)

            data = {s: stream_data[s] for s in STREAMS_OUT + ["error"]
                    if s in stream_data}
            data.update(activity)
            queue.put(data)
            # app.logger.debug("importing {}...queued!".format(activity["id"]))
            gevent.sleep(0)

        pool = pool or Pool(CONCURRENCY)
        client = self.client()

        #  If out_queue is not supplied then query_activities is blocking
        put_stopIteration = False
        if not out_queue:
            out_queue = Queue()
            put_stopIteration = True

        index_df = None
        if (summaries or limit or only_ids or after or before):
            activity_index = self.get_index()

            if activity_index:
                index_df = activity_index["index_df"]
                elapsed = (datetime.utcnow() -
                           activity_index["dt_last_indexed"]).total_seconds()

                # update the index if we need to
                if (not OFFLINE) and (elapsed > INDEX_UPDATE_TIMEOUT):
                    index_df = self.update_index(index_df)

                if (not activity_ids):
                     # only consider activities with a summary polyline
                    ids_df = (
                        index_df[index_df.summary_polyline.notnull()]
                        .set_index("ts_local")
                        .sort_index(ascending=False)
                        .id
                    )

                    if limit:
                        ids_df = ids_df.head(int(limit))

                    elif before or after:
                        #  get ids of activities in date-range
                        if after:
                            ids_df = ids_df[:after]
                        if before:
                            ids_df = ids_df[before:]

                    activity_ids = ids_df.tolist()

                index_df = index_df.astype(
                    Users.index_df_out_dtypes).set_index("id")

                if only_ids:
                    out_queue.put(activity_ids)
                    out_queue.put(StopIteration)
                    return out_queue

                def summary_gen():
                    for aid in activity_ids:
                        A = {"id": int(aid)}
                        if summaries:
                            A.update(index_df.loc[int(aid)].to_dict())
                        # app.logger.debug(A)
                        yield A
                gen = summary_gen()

            elif build_index:
                # There is no activity index and we are to build one
                if only_ids:
                    return ["build"]

                else:
                    gen = Queue()
                    gevent.spawn(self.build_index,
                                 gen,
                                 limit,
                                 after,
                                 before,
                                 activity_ids)
            else:
                # Finally, if there is no index and rather than building one
                # we are requested to get the summary data directily from Strava
                # app.logger.info(
                #     "{}: getting summaries from Strava without build"
                #     .format(self))
                gen = (
                    Activities.strava2dict(a)
                    for a in self.client().get_activities(
                        limit=limit,
                        before=before,
                        after=after)
                )

        for A in gen:
            if "stop_rendering" in A:
                pool.join()

            if "id" not in A:
                out_queue.put(A)
                continue

            if summaries:
                if ("bounds" not in A):
                    A["bounds"] = Activities.bounds(A["summary_polyline"])

                A["ts_local"] = str(A["ts_local"])

                # TODO: do this on the client
                A.update(Activities.atype_properties(A["type"]))

            if owner_id:
                A.update({"owner": self.id, "profile": self.profile})

            if not streams:
                out_queue.put(A)

            else:
                stream_data = Activities.get(A["id"])

                if stream_data:
                    A.update(stream_data)
                    if ("bounds" not in A):
                        A["bounds"] = Activities.bounds(A["polyline"])
                    out_queue.put(A)

                elif not OFFLINE:
                    pool.spawn(Activities.import_and_queue_streams,
                               client, out_queue, A)
                gevent.sleep(0)

        # If we are using our own queue, we make sure to put a stopIteration
        #  at the end of it so we have to wait for all import jobs to finish.
        #  If the caller supplies a queue, can return immediately and let them
        #   handle responsibility of adding the stopIteration.
        if put_stopIteration:
            pool.join()
            out_queue.put(StopIteration)

        return out_queue

    #  outputs a stream of activites of other Heatflask users that are
    #   considered by Strava to be part of a group-activity
    def related_activities(self, activity_id, streams=False,
                           pool=None, out_queue=None):
        client = self.client()

        put_stopIteration = True if not out_queue else False

        out_queue = out_queue or Queue()
        pool = pool or Pool(CONCURRENCY)

        trivial_list = []

        # First we put this activity
        try:
            A = client.get_activity(int(activity_id))
        except Exception as e:
            app.logger.info("Error getting this activity: {}".format(e))
        else:
            trivial_list.append(A)

        try:
            related_activities = list(
                client.get_related_activities(int(activity_id)))

        except Exception as e:
            app.logger.info("Error getting related activities: {}".format(e))
            return [{"error": str(e)}]

        for obj in itertools.chain(related_activities, trivial_list):
            if streams:
                owner = self.__class__.get(obj.athlete.id)

                if owner:
                    # the owner is a Heatflask user
                    A = Activities.strava2dict(obj)
                    A["ts_local"] = str(A["ts_local"])
                    A["owner"] = owner.id
                    A["profile"] = owner.profile
                    A["bounds"] = Activities.bounds(A["summary_polyline"])
                    A.update(Activities.atype_properties(A["type"]))

                    stream_data = Activities.get(obj.id)
                    if stream_data:
                        A.update(stream_data)
                        out_queue.put(A)
                    else:
                        pool.spawn(
                            Activities.import_and_queue_streams,
                            owner.client(), out_queue, A)
            else:
                # we don't care about activity streams
                A = Activities.strava2dict(obj)

                A["ts_local"] = str(A["ts_local"])
                A["profile"] = "/avatar/athlete/medium.png"
                A["owner"] = obj.athlete.id
                A["bounds"] = Activities.bounds(A["summary_polyline"])
                A.update(Activities.atype_properties(A["type"]))
                out_queue.put(A)

        if put_stopIteration:
            out_queue.put(StopIteration)

        return out_queue


class Indexes(object):

    @staticmethod
    def init(clear_cache=False):
        # drop the "indexes" collection
        mongodb.indexes.drop()

        if clear_cache:
            # delete all users from the Redis cache, since they have indexes
            keys_to_delete = redis.keys(Users.key("*"))
            if keys_to_delete:
                redis.delete(*keys_to_delete)

        # create new indexes collection
        mongodb.create_collection("indexes")

        timeout = app.config["STORE_INDEX_TIMEOUT"]
        result = mongodb.indexes.create_index(
            "dt_last_indexed",
            expireAfterSeconds=timeout
        )
        app.logger.info("initialized Indexes collection with")
        return result


#  Activities class is only a proxy to underlying data structures.
#  There are no Activity objects
class Activities(object):

    @classmethod
    def init(cls, clear_cache=False):
        # Create/Initialize Activity database
        try:
            result1 = mongodb.activities.drop()
        except Exception as e:
            app.logger.debug(
                "error deleting activities collection from MongoDB.\n{}"
                .format(e))
            result1 = e

        if clear_cache:
            to_delete = redis.keys(cls.cache_key("*"))
            if to_delete:
                result2 = redis.delete(*to_delete)
            else:
                result2 = None

        mongodb.create_collection("activities")

        timeout = app.config["STORE_ACTIVITIES_TIMEOUT"]
        result = mongodb["activities"].create_index(
            "ts",
            expireAfterSeconds=timeout
        )
        app.logger.info("initialized Activity collection")
        return result

    # This is a list of tuples specifying properties of the rendered objects,
    #  such as path color, speed/pace in description.  others can be added
    ATYPE_SPECS = [
        ("Ride", "speed", "#2B60DE"),  # Ocean Blue
        ("Run", "pace", "#FF0000"),  # Red
        ("Swim", "speed", "#00FF7F"),  # SpringGreen
        ("Hike", "pace", "#FF1493"),  # DeepPink
        ("Walk", "pace", "#FF00FF"),  # Fuchsia
        ("AlpineSki", None, "#800080"),  # Purple
        ("BackcountrySki", None, "#800080"),  # Purple
        ("Canoeing", None, "#FFA500"),  # Orange
        ("Crossfit", None, None),
        ("EBikeRide", "speed", "#0000CD"),  # MediumBlue
        ("Elliptical", None, None),
        ("IceSkate", "speed", "#663399"),  # RebeccaPurple
        ("InlineSkate", None, "#8A2BE2"),  # BlueViolet
        ("Kayaking", None, "#FFA500"),  # Orange
        ("Kitesurf", "speed", None),
        ("NordicSki", None, "#800080"),  # purple
        ("RockClimbing", None, "#4B0082"),  # Indigo
        ("RollerSki", "speed", "#800080"),  # Purple
        ("Rowing", "speed", "#FA8072"),  # Salmon
        ("Snowboard", None, "#00FF00"),  # Lime
        ("Snowshoe", "pace", "#800080"),  # Purple
        ("StairStepper", None, None),
        ("StandUpPaddling", None, None),
        ("Surfing", None, "#006400"),  # DarkGreen
        ("VirtualRide", "speed", "#1E90FF"),  # DodgerBlue
        ("WeightTraining", None, None),
        ("Windsurf", "speed", None),
        ("Workout", None, None),
        ("Yoga", None, None)
    ]

    ATYPE_MAP = {atype.lower(): {"path_color": color, "vtype": vtype}
                 for atype, vtype, color in ATYPE_SPECS}

    @classmethod
    def atype_properties(cls, atype):
        return cls.ATYPE_MAP.get(atype.lower()) or cls.ATYPE_MAP.get("workout")

    @staticmethod
    def bounds(poly):
        if poly:
            latlngs = polyline.decode(poly)

            lats = [ll[0] for ll in latlngs]
            lngs = [ll[1] for ll in latlngs]

            return {
                "SW": (min(lats), min(lngs)),
                "NE": (max(lats), max(lngs))
            }
        else:
            return {}

    @staticmethod
    def stream_encode(vals):
        diffs = [b - a for a, b in zip(vals, vals[1:])]
        encoded = []
        pair = None
        for a, b in zip(diffs, diffs[1:]):
            if a == b:
                if pair:
                    pair[1] += 1
                else:
                    pair = [a, 2]
            else:
                if pair:
                    if pair[1] > 2:
                        encoded.append(pair)
                    else:
                        encoded.extend(2 * [pair[0]])
                    pair = None
                else:
                    encoded.append(a)
        if pair:
            encoded.append(pair)
        else:
            encoded.append(b)
        return encoded

    @staticmethod
    def stream_decode(rll_encoded, first_value=0):
        running_sum = first_value
        out_list = [first_value]

        for el in rll_encoded:
            if isinstance(el, list) and len(el) == 2:
                val, num_repeats = el
                for i in xrange(num_repeats):
                    running_sum += val
                    out_list.append(running_sum)
            else:
                running_sum += el
                out_list.append(running_sum)

        return out_list

    @classmethod
    def strava2dict(cls, a):
        d = {
            "id": a.id,
            "name": a.name,
            "type": a.type,
            "summary_polyline": a.map.summary_polyline,
            "ts_UTC": str(a.start_date),
            "group": a.athlete_count,
            "ts_local": a.start_date_local,
            "total_distance": float(a.distance),
            "elapsed_time": int(a.elapsed_time.total_seconds()),
            "average_speed": float(a.average_speed),
            # "bounds": cls.bounds(a)
        }
        return d

    @staticmethod
    def cache_key(id):
        return "A:{}".format(id)

    @classmethod
    def set(cls, id, data, timeout=CACHE_ACTIVITIES_TIMEOUT):
        # cache it first, in case mongo is down
        packed = msgpack.packb(data)
        result1 = redis.setex(cls.cache_key(id), packed, timeout)

        document = {
            "ts": datetime.utcnow(),
            "mpk": Binary(packed)
        }
        try:
            result2 = mongodb.activities.update_one(
                {"_id": int(id)},
                {"$set": document},
                upsert=True)
        except Exception as e:
            result2 = None
            app.logger.debug("error writing activity {} to MongoDB: {}"
                             .format(id, e))
        return result1, result2

    @classmethod
    def get(cls, id, timeout=CACHE_ACTIVITIES_TIMEOUT):
        packed = None
        key = cls.cache_key(id)
        cached = redis.get(key)

        if cached:
            redis.expire(key, timeout)  # reset expiration timeout
            # app.logger.debug("got Activity {} from cache".format(id))
            packed = cached
        else:
            try:
                document = mongodb.activities.find_one_and_update(
                    {"_id": int(id)},
                    {"$set": {"ts": datetime.utcnow()}}
                )

            except Exception as e:
                app.logger.debug(
                    "error accessing activity {} from MongoDB:\n{}"
                    .format(id, e))
                return

            if document:
                packed = document["mpk"]
                redis.setex(key, packed, timeout)
                # app.logger.debug("got activity {} data from MongoDB".format(id))
        if packed:
            return msgpack.unpackb(packed)

    @classmethod
    def import_streams(cls, client, activity_id, stream_names,
                       timeout=CACHE_ACTIVITIES_TIMEOUT):

        streams_to_import = list(stream_names)
        if ("polyline" in stream_names):
            streams_to_import.append("latlng")
            streams_to_import.remove("polyline")
        try:
            streams = client.get_activity_streams(activity_id,
                                                  series_type='time',
                                                  types=streams_to_import)
        except Exception as e:
            msg = ("Can't import streams for activity {}:\n{}"
                   .format(activity_id, e))
            # app.logger.error(msg)
            return {"error": msg}

        activity_streams = {name: streams[name].data for name in streams}

        # Encode/compress latlng data into polyline format
        if "polyline" in stream_names:
            if "latlng" in activity_streams:
                activity_streams["polyline"] = polyline.encode(
                    activity_streams['latlng'])
            else:
                return {"error": "no latlng stream for activity {}".format(activity_id)}

        for s in ["time"]:
            # Encode/compress these streams
            if (s in stream_names) and (activity_streams.get(s)):
                if len(activity_streams[s]) < 2:
                    return {
                        "error": "activity {} has no stream '{}'"
                        .format(activity_id, s)
                    }

                try:
                    activity_streams[s] = cls.stream_encode(activity_streams[s])
                except Exception as e:
                    msg = ("Can't encode stream '{}' for activity {} due to '{}':\n{}"
                           .format(s, activity_id, e, activity_streams[s]))
                    app.logger.error(msg)
                    return {"error": msg}

        output = {s: activity_streams[s] for s in stream_names}
        cls.set(activity_id, output, timeout)
        return output

    @classmethod
    def import_and_queue_streams(cls, client, queue, activity):
        # app.logger.debug("importing {}".format(activity["id"]))

        stream_data = cls.import_streams(
            client, activity["id"], STREAMS_TO_CACHE)

        data = {s: stream_data[s] for s in STREAMS_OUT + ["error"]
                if s in stream_data}
        data.update(activity)
        queue.put(data)
        # app.logger.debug("importing {}...queued!".format(activity["id"]))
        gevent.sleep(0)

    @staticmethod
    def path_color(activity_type):
        color_list = [color for color, activity_types
                      in app.config["ANTPATH_ACTIVITY_COLORS"].items()
                      if activity_type.lower() in activity_types]

        return color_list[0] if color_list else ""


class EventLogger(object):

    @classmethod
    def init(cls, rebuild=True, size=app.config["MAX_HISTORY_BYTES"]):

        collections = mongodb.collection_names(include_system_collections=False)

        if ("history" in collections) and rebuild:
            all_docs = mongodb.history.find()
            mongodb.history_new.insert_many(all_docs)
            mongodb.create_collection("history_new",
                                      capped=True,
                                      # autoIndexId=False,
                                      size=size)
            mongodb.history_new.rename("history", dropTarget=True)
        else:
            mongodb.create_collection("history",
                                      capped=True,
                                      # autoIndexId=False,
                                      size=size)

        stats = mongodb.command("collstats", "history")
        cls.new_event(msg="rebuilt event log: {}".format(stats))

    @staticmethod
    def get_event(event_id):
        event = mongodb.history.find_one({"_id": ObjectId(event_id)})
        event["_id"] = str(event["_id"])
        return event

    @staticmethod
    def get_log(limit=0):
        events = list(
            mongodb.history.find(
                sort=[("$natural", pymongo.DESCENDING)]).limit(limit)
        )
        for e in events:
            e["_id"] = str(e["_id"])
        return events

    @staticmethod
    def new_event(**event):
        event["ts"] = datetime.utcnow()
        mongodb.history.insert_one(event)

    @classmethod
    def log_request(cls, flask_request_object, **args):
        req = flask_request_object
        args.update({
            "ip": req.access_route[-1],
            "agent": vars(req.user_agent),
        })
        cls.new_event(**args)


class Webhooks(object):
    client = stravalib.Client()
    credentials = {
        "client_id": app.config["STRAVA_CLIENT_ID"],
        "client_secret": app.config["STRAVA_CLIENT_SECRET"]
    }

    @classmethod
    def create(cls, callback_url):
        try:
            subs = cls.client.create_subscription(
                callback_url=callback_url,
                **cls.credentials
            )
        except Exception as e:
            return {"error": str(e)}

        if "subscription" not in mongodb.collection_names():
            mongodb.create_collection("subscription",
                                      capped=True,
                                      size=1 * 1024 * 1024)
        app.logger.debug("create_subscription returns {}".format(subs))
        return {"created": str(subs)}

    @classmethod
    def handle_subscription_callback(cls, args):
        return cls.client.handle_subscription_callback(args)

    @classmethod
    def delete(cls, subscription_id=None, delete_collection=False):
        if not subscription_id:
            subs_list = cls.list()
            if subs_list:
                subscription_id = subs_list.pop()
        if subscription_id:
            try:
                cls.client.delete_subscription(subscription_id,
                                               **cls.credentials)
            except Exception as e:
                return {"error": str(e)}

            if delete_collection:
                mongodb.subscription.drop()

            result = {"success": "deleted subscription {}".format(
                subscription_id)}
        else:
            result = {"error": "non-existent/incorrect subscription id"}
        app.logger.error(result)
        return result

    @classmethod
    def list(cls):
        subs = cls.client.list_subscriptions(**cls.credentials)
        return [sub.id for sub in subs]

    @classmethod
    def handle_update_callback(cls, update_raw):
        # if an archived index exists for the user whose data is being updated,
        #  we will update his/her index.
        user_id = int(update_raw["owner_id"])
        archived_activity_index = mongodb.indexes.find_one(
            {"_id": user_id}
        )

        updated = None
        if archived_activity_index:
            # an activity index assoiciated with this update was found in
            # mongoDB, so most likely we have the associated user.
            updated = False
            user = Users.get(user_id, timeout=60)
            if user:
                ids = [int(update_raw["object_id"])]
                user.activity_index = archived_activity_index
                gevent.spawn(user.update_index,
                             activity_ids=ids,
                             reset_ttl=False)
                gevent.sleep(0)
                updated = True

        obj = cls.client.handle_subscription_update(update_raw)

        doc = {
            "dt": datetime.utcnow(),
            "subscription_id": obj.subscription_id,
            "owner_id": obj.owner_id,
            "object_id": obj.object_id,
            "object_type": obj.object_type,
            "aspect_type": obj.aspect_type,
            "event_time": str(obj.event_time),
            "updated": updated
        }
        result = mongodb.subscription.insert_one(doc)
        return result

    @staticmethod
    def iter_updates(limit=0):
        updates = mongodb.subscription.find(
            sort=[("$natural", pymongo.DESCENDING)]
        ).limit(limit)

        for u in updates:
            u["_id"] = str(u["_id"])
            yield u


class Utility():

    @staticmethod
    def href(url, text):
        return "<a href='{}' target='_blank'>{}</a>".format(url, text)

    @staticmethod
    def ip_lookup_url(ip):
        return "http://freegeoip.net/json/{}".format(ip) if ip else "#"

    @staticmethod
    def ip_address(flask_request_object):
        return flask_request_object.access_route[-1]

    @classmethod
    def ip_lookup(cls, ip_address):
        r = requests.get(cls.ip_lookup_url(ip_address))
        return r.json()

    @classmethod
    def ip_timezone(cls, ip_address):
        tz = cls.ip_lookup(ip_address)["time_zone"]
        return tz if tz else 'America/Los_Angeles'

    @staticmethod
    def utc_to_timezone(dt, timezone='America/Los_Angeles'):
        from_zone = dateutil.tz.gettz('UTC')
        to_zone = dateutil.tz.gettz(timezone)
        utc = dt.replace(tzinfo=from_zone)
        return utc.astimezone(to_zone)


if "history" not in mongodb.collection_names():
    EventLogger.init()

if "activities" not in mongodb.collection_names():
    Activities.init()

if "indexes" not in mongodb.collection_names():
    Indexes.init()
