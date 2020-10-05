# type_audiovideocast.py

import threading
import logging
import datetime
import zipfile
import os
from xml.dom import minidom

from django.conf import settings
from django.template.defaultfilters import slugify
from django.core.files.base import ContentFile
from pod.video.models import Video, Type
from pod.video.encode import start_encode
from pod.enrichment.models import Enrichment
from ..models import Recording

USE_ADVANCED_RECORDER = getattr(settings, 'USE_ADVANCED_RECORDER', False)

DEFAULT_RECORDER_TYPE_ID = getattr(
    settings, 'DEFAULT_RECORDER_TYPE_ID',
    1
)

ENCODE_VIDEO = getattr(settings,
                       'ENCODE_VIDEO',
                       start_encode)

RECORDER_SKIP_FIRST_IMAGE = getattr(settings,
                                    'RECORDER_SKIP_FIRST_IMAGE',
                                    False)

if getattr(settings, 'USE_PODFILE', False):
    from pod.podfile.models import CustomImageModel
    from pod.podfile.models import UserFolder
    FILEPICKER = True
else:
    FILEPICKER = False
    from pod.main.models import CustomImageModel

log = logging.getLogger(__name__)


def process(recording):
    log.info("START PROCESS OF RECORDING %s" % recording)
    t = threading.Thread(target=encode_recording,
                         args=[recording])
    t.setDaemon(True)
    t.start()


def add_comment(recording_id, comment):
    recording = Recording.objects.get(id=recording_id)
    recording.comment = "%s\n%s" % (recording.comment, comment)
    recording.save()


def save_video(recording, video_data, video_src):
    video = Video()
    video.owner = recording.user
    video.type = Type.objects.get(id=DEFAULT_RECORDER_TYPE_ID)
    nom, ext = os.path.splitext(video_src)
    ext = ext.lower()
    video.video.save(
        "record_" + slugify(recording.title) + ext,
        ContentFile(video_data),
        save=False)
    # on recupere le nom du fichier sur le serveur
    video.title = recording.title
    video.save()
    for usr in recording.recorder.additional_users.all():
        video.additional_owners.add(usr)
    video.is_draft = recording.recorder.is_draft
    video.is_restricted = recording.recorder.is_restricted
    for g in recording.recorder.restrict_access_to_groups.all():
        video.restrict_access_to_groups.add(g)
    video.password = recording.recorder.password

    if USE_ADVANCED_RECORDER:
        TRANSCRIPT = getattr(settings, 'USE_TRANSCRIPTION', False)
        for c in recording.recorder.channel.all():
            video.channel.add(c)
        for t in recording.recorder.theme.all():
            video.theme.add(t)
        for d in recording.recorder.discipline.all():
            video.discipline.add(d)
        video.main_lang = recording.recorder.main_lang
        video.cursus = recording.recorder.cursus
        video.tags = recording.recorder.tags
        if TRANSCRIPT:
            video.transcript = recording.recorder.transcript
        video.licence = recording.recorder.licence
        video.allow_downloading = recording.recorder.allow_downloading
        video.is_360 = recording.recorder.is_360
        video.disable_comment = recording.recorder.disable_comment
    video.save()
    ENCODE_VIDEO(video.id)
    return video


def save_slide(data, filename, video, enrichment, recording):
    if len(data):
        slide_name, ext = os.path.splitext(
            os.path.basename(filename))
        if FILEPICKER:
            homedir, created = UserFolder.objects.get_or_create(
                name='home',
                owner=video.owner)
            videodir, created = UserFolder.objects.get_or_create(
                name='%s' % video.slug,
                owner=video.owner)
            previousImage = CustomImageModel.objects.filter(
                name__startswith=slugify(
                    video.title + "_" + slide_name),
                folder=videodir,
                created_by=video.owner
            )
            for img in previousImage:
                img.delete()
            image = CustomImageModel(
                folder=videodir,
                created_by=video.owner
            )
            image.file.save(
                slugify(video.title + "_" + slide_name) + ext,
                ContentFile(data),
                save=True)
            image.save()
        else:
            image = CustomImageModel()
            image.file.save(
                slugify(video.title + "_" + slide_name) + ext,
                ContentFile(data),
                save=True)
            image.save()
        enrichment.type = 'image'
        enrichment.image = image
        enrichment.save()
    else:
        add_comment(recording.id, "file %s is empty" % filename)


def save_enrichment(video, list_node_img, recording, media_name, zip):
    previousEnrichment = None
    i = 0
    Enrichment.objects.filter(video=video).delete()
    start_img = 1 if RECORDER_SKIP_FIRST_IMAGE else 0
    for item in list_node_img[start_img:]:  # skip the first
        i += 1
        add_comment(recording.id, ">> ITEM %s: %s" %
                    (i, item.getAttribute("src")))
        filename = media_name + \
            "/%s" % item.getAttribute("src")
        timecode = float("%s" % item.getAttribute("begin"))
        timecode = int(round(timecode))
        add_comment(recording.id, ">> timecode %s" % timecode)
        # Enrichment
        enrichment = Enrichment.objects.create(
            video=video,
            title='slide %s' % i,
            start=timecode,
            end=timecode + 1,
            stop_video=False,
        )
        # Enrichment Image
        data = zip.read(filename)
        save_slide(data, filename, video, enrichment, recording)
        if previousEnrichment is not None:
            previousEnrichment.end = timecode - 1 if (
                timecode - 1 > 0) else previousEnrichment.end
            previousEnrichment.save()
        previousEnrichment = enrichment

    video = Video.objects.get(id=video.id)
    if (previousEnrichment is not None
            and video.duration
            and video.duration > 0):
        previousEnrichment.end = video.duration
        previousEnrichment.save()


def get_video_source(xmldoc):
    if xmldoc.getElementsByTagName("audio"):
        return xmldoc.getElementsByTagName(
            "audio").item(0).getAttribute("src")
    if xmldoc.getElementsByTagName("video"):
        return xmldoc.getElementsByTagName(
            "video").item(0).getAttribute("src")
    return None


def open_zipfile(recording):
    try:
        zip = zipfile.ZipFile(recording.source_file)
        return zip
    except FileNotFoundError as e:
        add_comment(recording.id, "Error : %s" % e)
        return -1
    except zipfile.BadZipFile as e:
        add_comment(recording.id, "Error : %s" % e)
        return -1


def encode_recording(recording):
    recording.comment = ""
    recording.save()
    add_comment(recording.id, 'Start at %s\n--\n' % datetime.datetime.now())
    zip = open_zipfile(recording)
    if zip == -1:
        return -1

    media_name, ext = os.path.splitext(
        os.path.basename(recording.source_file))
    add_comment(recording.id, "> media name %s" % media_name)
    try:
        smil = zip.open(media_name + "/cours.smil")
        xmldoc = minidom.parse(smil)
        smil.close()
    except KeyError as e:
        add_comment(recording.id, "Error : %s" % e)
        zip.close()
        return -1

    video_src = get_video_source(xmldoc)

    if video_src:
        add_comment(recording.id, "> video file %s" % video_src)
        video_data = zip.read(media_name + "/%s" % video_src)
        video = save_video(recording, video_data, video_src)
        list_node_img = xmldoc.getElementsByTagName("img")
        add_comment(recording.id, "> slides found %s" % len(list_node_img))
        if len(list_node_img):
            save_enrichment(video, list_node_img, recording, media_name, zip)
        else:
            add_comment(recording.id, "No slides node found")
            zip.close()
            return -1
    else:
        add_comment(recording.id, "Error : No video source found")
        zip.close()
        return -1
    zip.close()
    add_comment(recording.id, 'End processing zip file')
    os.rename(recording.source_file, recording.source_file+"_treated")
