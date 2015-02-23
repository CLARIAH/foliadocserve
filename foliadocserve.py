#!/usr/bin/env python3

from __future__ import print_function, unicode_literals, division, absolute_import
import cherrypy
import argparse
import time
import os
import json
import random
import datetime
import subprocess
import sys
import traceback
from copy import copy
from collections import defaultdict
from pynlpl.formats import folia, fql

def fake_wait_for_occupied_port(host, port): return

class NoSuchDocument(Exception):
    pass

logfile = None
def log(msg):
    global logfile
    if logfile:
        logfile.write(msg+"\n")
        logfile.flush()


def parsegitlog(data):
    commit = None
    date = None
    msg = None
    for line in data.split("\n"):
        line = line.strip()
        if line[0:6] == 'commit':
            #yield previous
            if commit and date and msg:
                yield commit, date, msg
            commit = line[7:]
            msg = None
            date = None
        elif line[0:7] == 'Author:':
            pass
        elif line[0:5] == 'Date:':
            date = line[6:].strip()
        elif line:
            msg = line
    if commit and date and msg:
        yield commit, date, msg


class DocStore:
    def __init__(self, workdir, expiretime):
        log("Initialising document store in " + workdir)
        self.workdir = workdir
        self.expiretime = expiretime
        self.data = {}
        self.lastchange = {}
        self.updateq = defaultdict(dict) #update queue, (namespace,docid) => session_id => [folia element id], for concurrency
        self.lastaccess = defaultdict(dict) # (namespace,docid) => session_id => time
        self.setdefinitions = {}
        if os.path.exists(self.workdir + "/.git"):
            self.git = True
        else:
            self.git = False
        super().__init__()

    def getfilename(self, key):
        assert isinstance(key, tuple) and len(key) == 2
        return self.workdir + '/' + key[0] + '/' + key[1] + '.folia.xml'

    def load(self,key, forcereload=False):
        if key[0] == "testflat": key = ("testflat", "testflat")
        filename = self.getfilename(key)
        if not key in self or forcereload:
            if not os.path.exists(filename):
                log("File not found: " + filename)
                raise NoSuchDocument
            log("Loading " + filename)
            self.data[key] = folia.Document(file=filename, setdefinitions=self.setdefinitions, loadsetdefinitions=True)
            self.lastchange[key] = time.time()
        return self.data[key]



    def save(self, key, message = "unspecified change"):
        doc = self[key]
        if key[0] == "testflat":
            #No need to save the document, instead we run our tests:
            doc.save("/tmp/testflat.xml")
            return test(doc, key[1])
        else:
            log("Saving " + self.getfilename(key) + " - " + message)
            doc.save()
            if self.git:
                log("Doing git commit for " + self.getfilename(key) + " - " + message)
                os.chdir(self.workdir)
                r = os.system("git add " + self.getfilename(key) + " && git commit -m \"" + message + "\"")
                if r != 0:
                    log("Error during git add/commit of " + self.getfilename(key))


    def unload(self, key, save=True):
        if key in self:
            if save:
                self.save(key,"Saving unsaved changes")
            log("Unloading " + "/".join(key))
            del self.data[key]
            del self.lastchange[key]
        else:
            raise NoSuchDocument

    def __getitem__(self, key):
        assert isinstance(key, tuple) and len(key) == 2
        if key[0] == "testflat":
            key = ("testflat","testflat")
        self.load(key)
        return self.data[key]

    def __setitem__(self, key, doc):
        assert isinstance(key, tuple) and len(key) == 2
        assert isinstance(doc, folia.Document)
        doc.filename = self.getfilename(key)
        self.data[key] = doc

    def __contains__(self,key):
        assert isinstance(key, tuple) and len(key) == 2
        return key in self.data


    def __len__(self):
        return len(self.data)

    def keys(self):
        return self.data.keys()

    def items(self):
        return self.data.items()

    def values(self):
        return self.data.values()

    def __iter__(self):
        return iter(self.data)

    def autounload(self, save=True):
        unload = []
        for key, t in self.lastchange.items():
            if t > time.time() + self.expiretime:
                unload.append(key)

        for key in unload:
            self.unload(key, save)


def gethtml(element):
    """Converts the element to html skeleton"""
    if isinstance(element, folia.Correction):
        s = ""
        if element.hasnew():
            for child in element.new():
                if isinstance(child, folia.AbstractStructureElement) or isinstance(child, folia.Correction):
                    s += gethtml(child)
        elif element.hascurrent():
            for child in element.current():
                if isinstance(child, folia.AbstractStructureElement) or isinstance(child, folia.Correction):
                    s += gethtml(child)
        return s
    elif isinstance(element, folia.AbstractStructureElement):
        s = ""
        for child in element:
            if isinstance(child, folia.AbstractStructureElement) or isinstance(child, folia.Correction):
                s += gethtml(child)
        if not isinstance(element, folia.Text) and not isinstance(element, folia.Division):
            try:
                label = "<span class=\"lbl\">" + element.text() + "</span>"
            except folia.NoSuchText:
                label = "<span class=\"lbl\"></span>"
        else:
            label = ""
        if not isinstance(element,folia.Word) or (isinstance(element, folia.Word) and element.space):
            label += " "

        if not element.id:
            element.id = element.doc.id + "." + element.XMLTAG + ".id" + str(random.randint(1000,999999999))
        if s:
            s = "<div id=\"" + element.id + "\" class=\"F " + element.XMLTAG + "\">" + label + s
        else:
            s = "<div id=\"" + element.id + "\" class=\"F " + element.XMLTAG + " deepest\">" + label
        if isinstance(element, folia.Linebreak):
            s += "<br />"
        if isinstance(element, folia.Whitespace):
            s += "<br /><br />"
        elif isinstance(element, folia.Figure):
            s += "<img src=\"" + element.src + "\">"
        s += "</div>"
        if isinstance(element, folia.List):
            s = "<ul>" + s + "</ul>"
        elif isinstance(element, folia.ListItem):
            s = "<li>" + s + "</li>"
        elif isinstance(element, folia.Table):
            s = "<table>" + s + "</table>"
        elif isinstance(element, folia.Row):
            s = "<tr>" + s + "</tr>"
        elif isinstance(element, folia.Cell):
            s = "<td>" + s + "</td>"
        return s
    else:
        raise Exception("Structure element expected, got " + str(type(element)))

def getannotations(element, previouswordid = None):
    if isinstance(element, folia.Correction):
        if not element.id:
            #annotator requires IDS on corrections, make one on the fly
            hash = random.getrandbits(128)
            element.id = element.doc.id + ".correction.%032x" % hash
        correction_new = []
        correction_current = []
        correction_original = []
        correction_suggestions = []
        if element.hasnew():
            for x in element.new():
                for y in  getannotations(x):
                    if not 'incorrection' in y: y['incorrection'] = []
                    y['incorrection'].append(element.id)
                    correction_new.append(y)
                    yield y #yield as any other
        if element.hascurrent():
            for x in element.current():
                for y in  getannotations(x):
                    if not 'incorrection' in y: y['incorrection'] = []
                    y['incorrection'].append(element.id)
                    correction_current.append(y)
                    yield y #yield as any other
        if element.hasoriginal():
            for x in element.original():
                for y in  getannotations(x):
                    y['auth'] = False
                    if not 'incorrection' in y: y['incorrection'] = []
                    y['incorrection'].append(element.id)
                    correction_original.append(y)
        if element.hassuggestions():
            for x in element.suggestions():
                for y in  getannotations(x):
                    y['auth'] = False
                    if not 'incorrection' in y: y['incorrection'] = []
                    y['incorrection'].append(element.id)
                    correction_suggestions.append(y)

        annotation = {'id': element.id ,'set': element.set, 'class': element.cls, 'type': 'correction', 'new': correction_new,'current': correction_current, 'original': correction_original, 'suggestions': correction_suggestions}
        if element.annotator:
            annotation['annotator'] = element.annotator
        if element.annotatortype == folia.AnnotatorType.AUTO:
            annotation['annotatortype'] = "auto"
        elif element.annotatortype == folia.AnnotatorType.MANUAL:
            annotation['annotatortype'] = "manual"
        p = element.ancestor(folia.AbstractStructureElement)
        annotation['targets'] = [ p.id ]
        yield annotation
    elif isinstance(element, folia.AbstractTokenAnnotation) or isinstance(element,folia.TextContent):
        annotation = element.json()
        p = element.parent
        #log("Parent of " + str(repr(element))+ " is "+ str(repr(p)))
        p = element.ancestor(folia.AbstractStructureElement)
        annotation['targets'] = [ p.id ]
        assert isinstance(annotation, dict)
        yield annotation
    elif isinstance(element, folia.AbstractSpanAnnotation):
        if not element.id and (folia.Attrib.ID in element.REQUIRED_ATTRIBS or folia.Attrib.ID in element.OPTIONAL_ATTRIBS):
            #span annotation elements must have an ID for the editor to work with them, let's autogenerate one:
            element.id = element.doc.data[0].generate_id(element)
            #and add to index
            element.doc.index[element.id] = element
        annotation = element.json()
        annotation['span'] = True
        annotation['targets'] = [ x.id for x in element.wrefs() ]
        assert isinstance(annotation, dict)
        yield annotation
    if isinstance(element, folia.AbstractStructureElement):
        annotation =  element.json(None, False) #no recursion
        annotation['self'] = True #this describes the structure element itself rather than an annotation under it
        annotation['targets'] = [ element.id ]
        yield annotation
    if isinstance(element, folia.AbstractStructureElement) or isinstance(element, folia.AbstractAnnotationLayer) or isinstance(element, folia.AbstractSpanAnnotation) or isinstance(element, folia.Suggestion):
        for child in element:
            for x in getannotations(child, previouswordid):
                assert isinstance(x, dict)
                if previouswordid and not 'previousword' in x:
                    x['previousword'] = previouswordid
                yield x
            if isinstance(child, folia.Word):
                previouswordid = child.id

def getdeclarations(doc):
    for annotationtype, set in doc.annotations:
        try:
            C = folia.ANNOTATIONTYPE2CLASS[annotationtype]
        except KeyError:
            pass
        #if (issubclass(C, folia.AbstractAnnotation) or C is folia.TextContent or C is folia.Correction) and not (issubclass(C, folia.AbstractTextMarkup)): #rules out structure elements for now
        if not issubclass(C, folia.AbstractTextMarkup) and annotationtype in folia.ANNOTATIONTYPE2XML:
            annotationtype = folia.ANNOTATIONTYPE2XML[annotationtype]
            yield {'annotationtype': annotationtype, 'set': set}

def getsetdefinitions(doc):
    setdefs = {}
    for annotationtype, set in doc.annotations:
        if set in doc.setdefinitions:
            setdefs[set] = doc.setdefinitions[set].json()
    return setdefs



def parseactor(words, i):
    set = id = None
    if len(words) <= i+1:
        raise FQLParseError("Expected annotation type, got end of query")
    if words[i+1] in folia.XML2CLASS:
        annotationtype = words[i+1]
    else:
        raise FQLParseError("No such annotation type: " + words[i+1])
    if len(words) > i+3:
        if words[i+2] == 'OF':
            set = words[i+3]
            skipwords = 3
        elif words[i+2] == 'ID':
            id = words[i+3]
            skipwords = 3
        else:
            skipwords = 1
    else:
        skipwords = 1
    return annotationtype, set, id, skipwords

def parseassignments(words,i):
    skipwords = 0
    processedwords = 0
    assignments = {}
    for j, word in enumerate(words[i:]):
        if skipwords:
            skipwords -= 1
            processedwords += 1
            continue
        if word in ['FOR']:
            #end
            break
        elif word.lower() in ['class','annotator','annotatortype','id','n','text','insertleft','insertright']:
            type = word.lower()
            assignments[type] = words[i+j+1]
            skipwords += 1
        elif word.lower() == 'confidence':
            assignments['confidence'] = float(words[i+j+1])
            skipwords += 1
        elif word.lower() in ['split','merge']:
            type = word.lower()
            assignments[type] = True
        elif word == ",":
            processedwords += 1
            break
        else:
            raise FQLParseError("Unknown variable in WITH statement: " + word)

        processedwords += 1
    return assignments, processedwords





def getdocumentselector(query):
    if query.startswith("USE "):
        end = query[4:].index(' ') + 4
        if end >= 0:
            try:
                namespace,docid = query[4:end].split("/")
            except:
                raise fql.SyntaxError("USE statement takes namespace/docid pair")
            return (namespace,docid), query[end+1:]
        else:
            try:
                namespace,docid = query[4:end].split("/")
            except:
                raise fql.SyntaxError("USE statement takes namespace/docid pair")
            return (namespace,docid), ""
    return None, query

def parseresults(results):





class Root:
    def __init__(self,docstore,args):
        self.docstore = docstore
        self.workdir = args.workdir

    @cherrypy.expose
    def makenamespace(self, namespace):
        namepace = namespace.replace('/','').replace('..','')
        try:
            os.mkdir(self.workdir + '/' + namespace)
        except:
            pass
        cherrypy.response.headers['Content-Type']= 'text/plain'
        return "ok"

    ###NEW###



    @cherrypy.expose
    def query(self, namespace):
        if 'X-sessionid' in cherrypy.request.headers:
            sessionid = cherrypy.request.headers['X-sessionid']
        else:
            sessionid = 'NOSID'
        if 'query' in cherrypy.request.params:
            rawqueries = cherrypy.request.params['query'].split("\n")
        else:
            cl = cherrypy.request.headers['Content-Length']
            rawqueries = cherrypy.request.body.read(int(cl)).split("\n")

        prevdocselector = None
        for rawquery in rawqueries:
            try:
                docselector, rawquery = parsedocumentselector(rawquery)
                if not docselector: docselector = prevdocselector
                query = fql.Query(rawquery)
                if query.format == "python": query.format = "xml"
                if query.action and not docselector:
                    raise fql.SyntaxError("Document Server requires USE statement prior to FQL query")
            except fql.SyntaxError as e:
                raise cherrypy.HTTPError(404, "FQL syntax error: " + str(e))

            queries.append(query)
            prevdocselector = docselector

        results = []
        for query in queries:
            try:
                doc = self.docstore[docselector]
                results.append( query(doc,False) ) #False = nowrap
                format = query.format
            except NoSuchDocument:
                raise cherrypy.HTTPError(404, "Document not found: " + docselector[0] + "/" + docselector[1])
            except fql.ParseError as e:
                raise cherrypy.HTTPError(404, "FQL parse error: " + str(e))

        if formats.endswith('xml'):
            cherrypy.response.headers['Content-Type']= 'text/xml'
        elif formats.endswith('json'):
            cherrypy.response.headers['Content-Type']= 'application/json'

        if format == "xml":
            return "<results>" + "\n".join(results) + "</results>"
        elif format == "json":
            return "[" + ",".join(results) + "]"
        elif format == "flat":
            cherrypy.response.headers['Content-Type']= 'application/json'
            return parseresults(results)
        else:
            return results[0]


    ###OLD###

    @cherrypy.expose
    def getdoc(self, namespace, docid, sid):
        namepace = namespace.replace('/','').replace('..','')
        if sid[-5:] != 'NOSID':
            log("Creating session " + sid + " for " + "/".join((namespace,docid)))
            self.docstore.lastaccess[(namespace,docid)][sid] = time.time()
            self.docstore.updateq[(namespace,docid)][sid] = []
        try:
            log("Returning document " + "/".join((namespace,docid)) + " in session " + sid)
            cherrypy.response.headers['Content-Type'] = 'application/json'
            return json.dumps({
                'html': gethtml(self.docstore[(namespace,docid)].data[0]),
                'declarations': tuple(getdeclarations(self.docstore[(namespace,docid)])),
                'annotations': tuple(getannotations(self.docstore[(namespace,docid)].data[0])),
                'setdefinitions': getsetdefinitions(self.docstore[(namespace,docid)]),
            }).encode('utf-8')
        except NoSuchDocument:
            raise cherrypy.HTTPError(404, "Document not found: " + namespace + "/" + docid)



    @cherrypy.expose
    def getdochistory(self, namespace, docid):
        namepace = namespace.replace('/','').replace('..','').replace(';','').replace('&','')
        docid = docid.replace('/','').replace('..','').replace(';','').replace('&','')
        log("Returning history for document " + "/".join((namespace,docid)))
        cherrypy.response.headers['Content-Type'] = 'application/json'
        if self.docstore.git and (namespace,docid) in self.docstore:
            log("Invoking git log " + namespace+"/"+docid + ".folia.xml")
            os.chdir(self.workdir)
            proc = subprocess.Popen("git log " + namespace + "/" + docid + ".folia.xml", stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=True,cwd=self.workdir)
            outs, errs = proc.communicate()
            if errs: log("git log errors? " + errs.decode('utf-8'))
            d = {'history':[]}
            count = 0
            for commit, date, msg in parsegitlog(outs.decode('utf-8')):
                count += 1
                d['history'].append( {'commit': commit, 'date': date, 'msg':msg})
            if count == 0: log("git log output: " + outs.decode('utf-8'))
            log(str(count) + " revisions found - " + errs.decode('utf-8'))
            return json.dumps(d).encode('utf-8')
        else:
            return json.dumps({'history': []}).encode('utf-8')

    @cherrypy.expose
    def revert(self, namespace, docid, commithash):
        if not all([ x.isalnum() for x in commithash ]):
            return b"{}"

        cherrypy.response.headers['Content-Type'] = 'application/json'
        if self.docstore.git:
            if (namespace,docid) in self.docstore:
                os.chdir(self.workdir)
                #unload document (will even still save it if not done yet, cause we need a clean workdir)
                key = (namespace,docid)
                self.docstore.unload(key)

            log("Doing git revert for " + self.docstore.getfilename(key) )
            os.chdir(self.workdir)
            r = os.system("git checkout " + commithash + " " + self.docstore.getfilename(key) + " && git commit -m \"Reverting to commit " + commithash + "\"")
            if r != 0:
                log("Error during git revert of " + self.docstore.getfilename(key))
            return b"{}"
        else:
            return b"{}"


    @cherrypy.expose
    def annotate(self, namespace, requestdocid, sid):
        namepace = namespace.replace('/','').replace('..','')
        cl = cherrypy.request.headers['Content-Length']
        rawbody = cherrypy.request.body.read(int(cl))
        request = json.loads(str(rawbody,'utf-8'))
        returnresponse = {}
        log("Annotation action - Renewing session " + sid + " for " + "/".join((namespace,requestdocid)))

        if not 'queries' in request or len(request['queries']) == 0:
            response = {'error': "No queries passed"}
            return json.dumps(response)


        data = {}
        for query in request['queries']:
            try:
                data = parsequery(query, data)
            except FQLParseError as e:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                formatted_lines = traceback.format_exc().splitlines()
                response = {'error': "The FQL query could not be parsed: " + query + ". Error: " + str(e) + " -- " + "\n".join(formatted_lines) }
                traceback.print_tb(exc_traceback, limit=50, file=sys.stderr)
                return json.dumps(response)



        for ns, docid in data:
            if ns != namespace:
                raise cherrypy.HTTPError(403, "No permission to edit documents out of active namespace " + namespace)


            if docid == requestdocid:
                self.docstore.lastaccess[(ns,docid)][sid] = time.time()

            doc = self.docstore[(ns,docid)]

            if 'annotatortype' in request:
                if request['annotatortype'] == 'auto':
                    annotatortype = folia.AnnotatorType.AUTO
                else:
                    annotatortype = folia.AnnotatorType.MANUAL
            else:
                annotatortype = folia.AnnotatorType.MANUAL

            annotationdata = { 'edits': data[(ns,docid)], 'annotator': request['annotator'], 'annotatortype': annotatortype }
            try:
                response = doannotation(doc, annotationdata)
            except Exception as e:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                formatted_lines = traceback.format_exc().splitlines()
                response = {'error': "The document server returned an error: " + str(e) + " -- " + "\n".join(formatted_lines) }
                traceback.print_tb(exc_traceback, limit=50, file=sys.stderr)
                return json.dumps(response)




            if docid == requestdocid:
                returnresponse = response

            if 'error' in response and response['error']:
                log("ERROR: " + response['error'])
                return json.dumps(response)

            if 'log' in response:
                response['log'] += " in document " + "/".join((ns,docid))
            else:
                if 'returnelementids' in response:
                    response['log'] = "Unknown edit by " + request['annotator'] + " in " + ",".join(response['returnelementids']) + " in " + "/".join((ns,docid))
                else:
                    response['log'] = "Unknown edit by " + request['annotator'] + " in " + "/".join((ns,docid))

            if ns == "testflat":
                testresult = self.docstore.save((ns,docid),response['log'] )
                log("Test result: " +str(repr(testresult)))
            else:
                self.docstore.save((ns,docid),response['log'] )
                testresult = None

            #set concurrency:
            if 'returnelementids' in response:
                for s in self.docstore.updateq[(ns,docid)]:
                    if s != sid:
                        log("Scheduling update for " + s)
                        for eid in response['returnelementids']:
                            self.docstore.updateq[(ns,docid)][s].append(eid)

        if 'returnelementids' in returnresponse:
            result =  self.getelements(namespace,requestdocid, returnresponse['returnelementids'],sid, testresult, {'queries': request['queries']})
        else:
            result = self.getelements(namespace,requestdocid, [self.docstore[(namespace,requestdocid)].data[0].id],sid, testresult,{'queries': request['queries']}) #return all
        if namespace == "testflat":
            #unload the document, we want a fresh copy every time
            log("Unloading test document")
            del self.docstore.data[(namespace,"testflat")]
        return result


    def checkexpireconcurrency(self):
        #purge old buffer
        deletelist = []
        for d in self.docstore.updateq:
            if d in self.docstore.lastaccess:
                for s in self.docstore.updateq[d]:
                    if s in self.docstore.lastaccess[d]:
                        lastaccess = self.docstore.lastaccess[d][s]
                        if time.time() - lastaccess > 3600*12:  #expire after 12 hours
                            deletelist.append( (d,s) )
        for d,s in deletelist:
            log("Expiring session " + s + " for " + "/".join(d))
            del self.docstore.lastaccess[d][s]
            del self.docstore.updateq[d][s]
            if len(self.docstore.lastaccess[d]) == 0:
                del self.docstore.lastaccess[d]
            if len(self.docstore.updateq[d]) == 0:
                del self.docstore.updateq[d]





    def getelements(self, namespace, docid, elementids, sid, testresult=None, response = {}):
        assert isinstance(elementids, list) or isinstance(elementids, tuple)
        response['elements'] = []
        if testresult:
            response['testresult'] = bool(testresult[0])
            response['testmessage'] = testresult[1]

        for elementid in elementids:
            log("Returning element " + str(elementid) + " in document " + "/".join((namespace,docid)) + ", session " + sid)
            namepace = namespace.replace('/','').replace('..','')
            if sid[-5:] != 'NOSID':
                self.docstore.lastaccess[(namespace,docid)][sid] = time.time()
                if sid in self.docstore.updateq[(namespace,docid)]:
                    try:
                        self.docstore.updateq[(namespace,docid)][sid].remove(elementid)
                    except:
                        pass
            try:
                cherrypy.response.headers['Content-Type'] = 'application/json'
                if elementid and elementid in self.docstore[(namespace,docid)]:
                    log("Request element: "+ elementid)
                    response['elements'].append({
                        'elementid': elementid,
                        'html': gethtml(self.docstore[(namespace,docid)][elementid]),
                        'annotations': tuple(getannotations(self.docstore[(namespace,docid)][elementid])),
                    })
            except NoSuchDocument:
                raise cherrypy.HTTPError(404, "Document not found: " + namespace + "/" + docid)
        return json.dumps(response).encode('utf-8')


    @cherrypy.expose
    def getelement(self, namespace, docid, elementid, sid):
        return self.getelements(namespace, docid, [elementid], sid)

    @cherrypy.expose
    def poll(self, namespace, docid, sid):
        if namespace == "testflat":
            return "{}" #no polling for testflat

        self.checkexpireconcurrency()
        if sid in self.docstore.updateq[(namespace,docid)]:
            ids = self.docstore.updateq[(namespace,docid)][sid]
            self.docstore.updateq[(namespace,docid)][sid] = []
            if ids:
                cherrypy.log("Succesful poll from session " + sid + " for " + "/".join((namespace,docid)) + ", returning IDs: " + " ".join(ids))
                return self.getelements(namespace,docid, ids, sid)
            else:
                return "{}"
        else:
            return "{}"



    @cherrypy.expose
    def declare(self, namespace, docid, sid):
        cl = cherrypy.request.headers['Content-Length']
        rawbody = cherrypy.request.body.read(int(cl))
        data = json.loads(str(rawbody,'utf-8'))
        log("Declaration: " + data['set'] + " for " + "/".join((namespace,docid)))
        self.docstore.lastaccess[(namespace,docid)][sid] = time.time()
        doc = self.docstore[(namespace,docid)]
        Class = folia.XML2CLASS[data['annotationtype']]
        doc.declare(Class, set=data['set'])
        return json.dumps({
                'declarations': tuple(getdeclarations(self.docstore[(namespace,docid)])),
                'setdefinitions': getsetdefinitions(self.docstore[(namespace,docid)])
        })



    @cherrypy.expose
    def getnamespaces(self):
        namespaces = [ x for x in os.listdir(self.docstore.workdir) if x != "testflat" and x[0] != "." ]
        return json.dumps({
                'namespaces': namespaces
        })

    @cherrypy.expose
    def getdocuments(self, namespace):
        namepace = namespace.replace('/','').replace('..','')
        docs = [ x for x in os.listdir(self.docstore.workdir + "/" + namespace) if x[-10:] == ".folia.xml" ]
        return json.dumps({
                'documents': docs,
                'timestamp': { x:os.path.getmtime(self.docstore.workdir + "/" + namespace + "/"+ x) for x in docs  },
                'filesize': { x:os.path.getsize(self.docstore.workdir + "/" + namespace + "/"+ x) for x in docs  }
        })


    @cherrypy.expose
    def getdocjson(self, namespace, docid, **args):
        namepace = namespace.replace('/','').replace('..','')
        try:
            cherrypy.response.headers['Content-Type']= 'application/json'
            return json.dumps(self.docstore[(namespace,docid)].json()).encode('utf-8')
        except NoSuchDocument:
            raise cherrypy.HTTPError(404, "Document not found: " + namespace + "/" + docid)

    @cherrypy.expose
    def getdocxml(self, namespace, docid, **args):
        namepace = namespace.replace('/','').replace('..','')
        try:
            cherrypy.response.headers['Content-Type']= 'text/xml'
            return self.docstore[(namespace,docid)].xmlstring().encode('utf-8')
        except NoSuchDocument:
            raise cherrypy.HTTPError(404, "Document not found: " + namespace + "/" + docid)

    @cherrypy.expose
    def upload(self, namespace):
        log("In upload, namespace=" + namespace)
        response = {}
        cl = cherrypy.request.headers['Content-Length']
        data = cherrypy.request.body.read(int(cl))
        cherrypy.response.headers['Content-Type'] = 'application/json'
        #data =cherrypy.request.params['data']
        try:
            log("Loading document from upload")
            doc = folia.Document(string=data,setdefinitions=self.docstore.setdefinitions, loadsetdefinitions=True)
            response['docid'] = doc.id
            self.docstore[(namespace,doc.id)] = doc
        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            formatted_lines = traceback.format_exc().splitlines()
            traceback.print_tb(exc_traceback, limit=50, file=sys.stderr)
            response['error'] = "Uploaded file is no valid FoLiA Document: " + str(e) + " -- " "\n".join(formatted_lines)
            log(response['error'])
            return json.dumps(response).encode('utf-8')

        filename = self.docstore.getfilename( (namespace, doc.id))
        i = 1
        while os.path.exists(filename):
            filename = self.docstore.getfilename( (namespace, doc.id + "." + str(i)))
            i += 1
        self.docstore.save((namespace,doc.id), "Initial upload")
        return json.dumps(response).encode('utf-8')

def testequal(value, reference, testmessage,testresult=True):
    if value == reference:
        testmessage = testmessage + ": Ok!\n"
        if testresult:
            testresult = True
    else:
        testmessage = testmessage + ": Failed! Value \"" + str(value) + "\" does not match reference \"" + str(reference) + "\"\n"
        testresult = False
    return testresult, testmessage


def test(doc, testname, testmessage = ""):
    log("Running test " + testname)

    #load clean document
    #perform test
    testresult = True #must start as True for chaining
    try:
        if testname in ( "textchange", "correction_textchange"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.1.w.2'].text(),"mijn", testmessage + "Testing text", testresult)
        elif testname in ( "textmerge","correction_textmerge"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.1.w.14'].text(),"wegreden", testmessage + "Testing text", testresult)
        elif testname in ("multiannotchange"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.6.w.8'].text(),"het", testmessage + "Testing text", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.6.w.8'].pos(),"LID(onbep,stan,rest)", testmessage + "Testing pos class", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.6.w.8'].lemma(),"het", testmessage + "Testing lemma class", testresult)
        elif testname in ("correction_tokenannotationchange"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.6.w.8'].pos(),"LID(onbep,stan,rest)", testmessage + "Testing pos class", testresult)
        elif testname in ("addentity", "correction_addentity"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.1.entity.1'].cls,"per", testmessage + "Testing presence of new entity", testresult)
            testresult, testmessage = testequal(len(doc['untitleddoc.p.3.s.1.entity.1'].wrefs()),2, testmessage + "Testing span size", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.1.entity.1'].wrefs(0).id, 'untitleddoc.p.3.s.1.w.12' , testmessage + "Testing order (1/2)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.1.entity.1'].wrefs(1).id, 'untitleddoc.p.3.s.1.w.12b' , testmessage + "Testing order (2/2)", testresult)
        elif testname in  ("worddelete"):
            testresult, testmessage = testequal('untitleddoc.p.3.s.8.w.10' in doc,False, testmessage + "Testing absence of word in index", testresult)
        elif testname in ( "wordsplit"):
            testresult, testmessage = testequal('untitleddoc.p.3.s.12.w.5' in doc,False, testmessage + "Testing absence of original word in index", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.18'].text(),"4", testmessage + "Testing new word (1/2)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.17'].text(),"uur", testmessage + "Testing new word (2/2)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.18'].next().id,"untitleddoc.p.3.s.12.w.17", testmessage + "Testing order (1/2)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.4'].next().id,"untitleddoc.p.3.s.12.w.18", testmessage + "Testing order (2/2)", testresult)
        elif testname in ("wordinsertionright", "correction_wordinsertionright"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.1'].text(),"en", testmessage + "Testing original word", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.17'].text(),"we", testmessage + "Testing new word", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.1'].next().id,"untitleddoc.p.3.s.12.w.17", testmessage + "Testing order", testresult)
        elif testname in ("wordinsertionleft", "correction_wordinsertionleft"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.13.w.12'].text(),"hoorden", testmessage + "Testing original word", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.13.w.16'].text(),"we", testmessage + "Testing new word", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.13.w.16'].next().id,"untitleddoc.p.3.s.13.w.12", testmessage + "Testing order", testresult)
        elif testname in ("spanchange"):
            testresult, testmessage = testequal(len(doc['untitleddoc.p.3.s.9.entity.1'].wrefs()),3, testmessage + "Testing span size", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.1'].wrefs(0).id, 'untitleddoc.p.3.s.9.w.7' , testmessage + "Testing order (1/3)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.1'].wrefs(1).id, 'untitleddoc.p.3.s.9.w.8' , testmessage + "Testing order (2/3)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.1'].wrefs(2).id, 'untitleddoc.p.3.s.9.w.9' , testmessage + "Testing order (3/3)", testresult)
        elif testname in ( "newoverlapspan", "correction_newoverlapspan"):
            testresult, testmessage = testequal(len(doc['untitleddoc.p.3.s.9.entity.1'].wrefs()),2, testmessage + "Testing original span size", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.1'].wrefs(0).id, 'untitleddoc.p.3.s.9.w.8' , testmessage + "Testing original entity", testresult)
            testresult, testmessage = testequal(len(doc['untitleddoc.p.3.s.9.entity.2'].wrefs()),3, testmessage + "Testing extra span size", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.2'].wrefs(0).id, 'untitleddoc.p.3.s.9.w.7' , testmessage + "Testing extra entity", testresult)
        elif testname in ( "spandeletion"):
            testresult, testmessage = testequal('untitleddoc.p.3.s.9.entity.1' in doc,False, testmessage + "Testing absence of entity in index", testresult)
        elif testname in ( "tokenannotationdeletion", "correction_tokenannotationdeletion"):
            exceptionraised = False
            try:
                doc['untitleddoc.p.3.s.8.w.4'].lemma()
            except folia.NoSuchAnnotation:
                exceptionraised = True
            testresult, testmessage = testequal(exceptionraised,True, testmessage + "Testing absence of lemma", testresult)
        elif testname in  ("correction_worddelete"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.8.correction.1'].original(0).id, 'untitleddoc.p.3.s.8.w.10',  testmessage + "Testing whether original word is now under original in correction", testresult)
        elif testname in ( "correction_wordsplit"):
            #entity ID will be different!
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.correction.1'].original(0).id, 'untitleddoc.p.3.s.12.w.5',  testmessage + "Testing whether original word is now under original in correction", testresult)

            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.17'].text(),"4", testmessage + "Testing new word (1/2)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.18'].text(),"uur", testmessage + "Testing new word (2/2)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.17'].next().id,"untitleddoc.p.3.s.12.w.18", testmessage + "Testing order (1/2)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.12.w.4'].next().id,"untitleddoc.p.3.s.12.w.17", testmessage + "Testing order (2/2)", testresult)
        elif testname in ( "correction_wordinsertionright", "correction_wordinsertionleft"):
            pass
        elif testname in ("correction_spanchange"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.correction.1'].original(0).id, 'untitleddoc.p.3.s.9.entity.1',  testmessage + "Testing whether original span is now under original in correction", testresult)
            testresult, testmessage = testequal(len(doc['untitleddoc.p.3.s.9.entity.2'].wrefs()),3, testmessage + "Testing span size", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.2'].wrefs(0).id, 'untitleddoc.p.3.s.9.w.7' , testmessage + "Testing order (1/3)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.2'].wrefs(1).id, 'untitleddoc.p.3.s.9.w.8' , testmessage + "Testing order (2/3)", testresult)
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.entity.2'].wrefs(2).id, 'untitleddoc.p.3.s.9.w.9' , testmessage + "Testing order (3/3)", testresult)
        elif testname in ( "correction_spandeletion"):
            testresult, testmessage = testequal(doc['untitleddoc.p.3.s.9.correction.1'].original(0).id, 'untitleddoc.p.3.s.9.entity.1',  testmessage + "Testing whether original span is now under original in correction", testresult)
        else:
            testresult = False
            testmessage += "No such test: " + testname
    except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            formatted_lines = traceback.format_exc().splitlines()
            testresult = False
            testmessage += "Test raised Exception in backend: " + str(e) + " -- " "\n".join(formatted_lines)


    return (testresult, testmessage)


def main():
    global logfile
    parser = argparse.ArgumentParser(description="", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-d','--workdir', type=str,help="Work directory", action='store',required=True)
    parser.add_argument('-p','--port', type=int,help="Port", action='store',default=8080,required=False)
    parser.add_argument('-l','--logfile', type=str,help="Log file", action='store',default="foliadocserve.log",required=False)
    parser.add_argument('--expirationtime', type=int,help="Expiration time in seconds, documents will be unloaded from memory after this period of inactivity", action='store',default=900,required=False)
    args = parser.parse_args()
    logfile = open(args.logfile,'w',encoding='utf-8')
    os.chdir(args.workdir)
    #args.storeconst, args.dataset, args.num, args.bar
    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': args.port,
    })
    cherrypy.process.servers.wait_for_occupied_port = fake_wait_for_occupied_port
    docstore = DocStore(args.workdir, args.expirationtime)
    cherrypy.quickstart(Root(docstore,args))

if __name__ == '__main__':
    main()
