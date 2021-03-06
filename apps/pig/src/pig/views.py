# Licensed to Hortonworks, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Hortonworks, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import re
import simplejson as json
from datetime import datetime

from django.utils.translation import ugettext as _
from django.core.urlresolvers import reverse
from django.shortcuts import redirect, get_object_or_404
from django.http import HttpResponse, Http404

from desktop.lib.exceptions import PopupException
from desktop.lib.django_util import login_notrequired, render
from filebrowser.views import _do_newfile_save
from pig.models import PigScript, UDF, Job
from pig.templeton import Templeton
from pig.forms import PigScriptForm, UDFForm
from pig.pig_client import PigClient

UDF_PATH = '/tmp/udfs/'
FILE_PATTERN = re.compile(r"[^\w\d_\-\.]+")


def index(request, obj_id=None):
    result = {}
    result['scripts'] = PigScript.objects.filter(saved=True, user=request.user)
    result['udfs'] = UDF.objects.all()
    if request.method == 'POST':
        form = PigScriptForm(request.POST)
        if not form.is_valid():
            raise PopupException(
                "".join(["%s: %s" % (field, error) for field, error in form.errors.iteritems()])
            )
        if "autosave" in request.session:
            del request.session['autosave']
        if request.POST.get("script_id"):
            instance = PigScript.objects.get(pk=request.POST['script_id'])
            form = PigScriptForm(request.POST, instance=instance)
            form.save()
        else:
            instance = PigScript(**form.cleaned_data)
            instance.user = request.user
            instance.save()
        return redirect(reverse("view_script", args=[instance.pk]))
    if not request.GET.get("new"):
        result.update(request.session.get("autosave", {}))

    #If we have obj_id, we renew or get instance to send it into form.
    if obj_id:
        instance = get_object_or_404(PigScript, pk=obj_id)
        for field in instance._meta.fields:
            result[field.name] = getattr(instance, field.name)
    return render('edit_script.mako', request, dict(result=result))


def explain(request):
    script_path = '/tmp/%s.pig' % re.sub(FILE_PATTERN, "",request.POST['title'])
    pig_src = request.POST['pig_script']
    pig_src = process_pig_script(pig_src, request)
    pig_src = augmate_python_path(request.POST.get("python_script"), pig_src)
    if request.GET.get('t_s') == 'Explain':
        pig = PigClient("pig -e explain -script %s" % script_path, script_path, pig_src)
    else:
        pig = PigClient("pig -x local -check %s" % script_path, script_path, pig_src)
    return HttpResponse(json.dumps({"text": pig.returnCode().replace("\n", "<br>")}))


#Making normal path to our *.jar files
udf_template = re.compile(r"register\s+(\S+\.jar)", re.I|re.M)
parameters_template = re.compile(r"%(\w+)%")
macro_template = re.compile(r"import\s+[\"\'](/\S+\.(?:macro|pig))[\"\']\s*;?", re.I|re.M)
def process_pig_script(pig_src, request):
    #1) Replace parameters with their values
    def get_param(matchobj):
        return request.POST.get("%" + matchobj.group(1) + "%")

    def get_file_path(matchobj):
        return "REGISTER " + request.fs.fs_defaultfs + UDF_PATH + matchobj.group(1)

    def get_macro_path(matchobj):
        return "IMPORT '" + request.fs.fs_defaultfs + matchobj.group(1) +"';"

    pig_src = re.sub(parameters_template, get_param, pig_src)
    pig_src = re.sub(udf_template, get_file_path, pig_src)
    pig_src = re.sub(macro_template, get_macro_path, pig_src)
    return pig_src


#Deleting PigScript objects
def delete(request, obj_id):
    instance = get_object_or_404(PigScript, pk=obj_id)
    instance.delete()
    return redirect(index)


#Clone script by obj_id to user forms
def script_clone(request, obj_id):
    script = get_object_or_404(PigScript, pk=obj_id)
    request.session['autosave'] = {
        "pig_script": script.pig_script,
        "python_script": script.python_script,
        "title": script.title + "(copy)"
    }
    return redirect(reverse("root_pig"))


def piggybank(request, obj_id=False):
    if request.method == 'POST':
        form = UDFForm(request.POST, request.FILES)
        if not form.is_valid():
            raise PopupException(_("Error in upload form: %s") % form.errors)

        uploaded_file = request.FILES['hdfs_file']
        dest = request.fs.join(UDF_PATH, re.sub(FILE_PATTERN, "", uploaded_file.name))
        tmp_file = uploaded_file.get_temp_path()
        username = request.user.username

        try:
            # Temp file is created by superuser. Chown the file.
            request.fs.do_as_superuser(request.fs.chmod, tmp_file, 0644)
            request.fs.do_as_superuser(request.fs.chown, tmp_file, username, username)

            # Move the file to where it belongs
            request.fs.rename(uploaded_file.get_temp_path(), dest)
        except IOError, ex:
            already_exists = False
            try:
                already_exists = request.fs.exists(dest)
                msg = _('Destination %(name)s already exists.' % {'name': dest})
            except Exception:
                msg = _('Copy to "%(name)s failed: %(error)s') % {'name': dest, 'error': ex}
            raise PopupException(msg)

        UDF.objects.create(url=dest, file_name=uploaded_file.name,
                           owner=request.user, description='111')
        return redirect(request.META.get("HTTP_REFERER", reverse("root_pig")))


def udf_del(request, obj_id):
    udf = get_object_or_404(UDF, pk=obj_id)
    try:
        request.fs.remove(udf.url)
        udf.delete()
    except:
        raise PopupException("Can't delete %s from HDFS, check if it exist." % udf.url)
    return redirect(request.META.get("HTTP_REFERER", reverse("root_pig")))


python_template = re.compile(r"(\w+)\.py")
def augmate_python_path(python_script, pig_script):
    with open('/tmp/python_udf.py', 'w') as f:
        f.write(python_script)
    return re.sub(python_template, '/tmp/python_udf.py', pig_script)


def start_job(request):
    if "autosave" in request.session:
        del request.session['autosave']
    t = Templeton(request.user.username)
    statusdir = "/tmp/.pigjobs/%s" % datetime.now().strftime("%s")
    script_file = statusdir + "/script.pig"
    pig_script = request.POST['pig_script']
    if request.POST.get("python_script"):
        pig_script = augmate_python_path(request.POST.get("python_script"), pig_script)
    pig_script = process_pig_script(pig_script, request)
    _do_newfile_save(request.fs, script_file, pig_script, "utf-8")
    job = t.pig_query(pig_file=script_file, statusdir=statusdir, callback=request.build_absolute_uri("/pig/notify/$jobId/"))

    if request.POST.get("script_id"):
        script = PigScript.objects.get(pk=request.POST['script_id'])
    else:
        script = PigScript(user=request.user, saved=False, title=request.POST['title'])
    script.pig_script = request.POST['pig_script']
    script.python_script = request.POST['python_script']
    script.save()
    Job.objects.create(job_id=job['id'],
                       statusdir=statusdir,
                       script=script,
                       email_notification=bool(request.POST['email']))
    return HttpResponse(
        json.dumps(
            {
                "job_id": job['id'],
                "text": "The Job <a href='%s' target='_blank'>%s</a> has been started successfully. <br>\
                You can always go back to <a href='%s'>Query History</a> for results after the run." % (
                    reverse("single_job", args=[job['id']]),
                    job['id'],
                    reverse("query_history")
                )
            }
        )
    )


def kill_job(request):
    t = Templeton(request.user.username)
    try:
        job_id = request.POST['job_id']
        t.kill_job(job_id)
        Job.objects.get(job_id=request.POST['job_id']).delete()
        return HttpResponse(json.dumps({"text": "Job %s was killed" % job_id}))
    except:
        return HttpResponse(json.dumps({"text": "An error was occured"}))


def _job_result(request, job):
    """

    """
    statusdir = job.statusdir
    result = {}
    try:
        error = request.fs.open(statusdir + "/stderr", "r")
        result['error'] = error.read()
        stdout = request.fs.open(statusdir + "/stdout", "r")
        result['stdout'] = stdout.read()
        exit_code = request.fs.open(statusdir + "/exit", "r")
        result['exit'] = exit_code.read()
        error.close()
        stdout.close()
        exit_code.close()
    except:
        raise Http404("Script data has been deleted.")
    return result


def get_job_result(request):
    job = Job.objects.get(job_id=request.POST['job_id'])
    result = {}
    try:
        result.update(_job_result(request, job))
    except:
        result['error'] = ""
        result['stdout'] = ""
        result['exit'] = ""
    return HttpResponse(json.dumps(result))


def query_history(request):
    return render(
        "query_history.mako",
        request,
        dict(jobs=Job.objects.filter(script__user=request.user).order_by("-start_time").all())
    )


def show_job_result(request, job_id):
    result = {}
    result['scripts'] = PigScript.objects.filter(saved=True, user=request.user)
    result['udfs'] = UDF.objects.all()
    job = Job.objects.get(job_id=job_id)
    result['job_id'] = job.job_id
    if job.email_notification:
        result['email_notification'] = True
    if job.status == job.JOB_SUBMITED:
        result['JOB_SUBMITED'] = True
    else:
        result.update(_job_result(request, job))
        result['stdout'] = result['stdout'].decode("utf-8")
    instance = job.script
    for field in instance._meta.fields:
        result[field.name] = getattr(instance, field.name)

    return render('edit_script.mako', request, dict(result=result))


def delete_job_object(request, job_id):
    try:
        job = Job.objects.get(job_id=job_id)
    except:
        raise Http404("This job doesn't exist.'")
    else:
        request.fs.rmtree(job.statusdir)
        job.delete()
    return redirect(reverse("query_history"))


@login_notrequired
def notify_job_complited(request, job_id):
    job = Job.objects.get(job_id=job_id)
    job.status = job.JOB_COMPLETED
    job.save()
    job_result = _job_result(request, job)
    if job.email_notification:
        subject = 'Query result'
        body = "Your PIG script '%s' started at %s has been complited.\n\
        You can check result at the following link %s" % (
            job.script.title,
            job.start_time.strftime('%d.%m.%Y %H:%M'),
            request.build_absolute_uri(reverse("show_job_result", args=[job_id]))
        )
        job.script.user.email_user(subject, body)
    return HttpResponse("Done")


def autosave_scripts(request):
    request.session['autosave'] = {
        "pig_script": request.POST['pig_script'],
        "python_script": request.POST.get('python_script'),
        "title": request.POST.get("title")
    }
    return HttpResponse(json.dumps("Done"))


def check_script_title(request):
    """
    Check script name for unique.
    """
    result = PigScript.objects.filter(title=request.GET.get("title"), saved=True, user=request.user).count()
    return HttpResponse(json.dumps(not bool(result)))


def download_job_result(request, job_id):
    job = get_object_or_404(Job, job_id=job_id)
    if job.status != job.JOB_COMPLETED:
        raise PopupException("Job not completed yet")
    job_result = _job_result(request, job)
    response = HttpResponse(job_result['stdout'], content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename="%s_result.txt"' % job_id
    return response
