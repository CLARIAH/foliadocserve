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
from pynlpl.formats import folia

def fake_wait_for_occupied_port(host, port): return

class NoSuchDocument(Exception):
    pass

class FQLParseError(Exception):
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



def parsequery(query, data = {}):
    """Parse FQL query"""

    log("Parsing FQL query: " + query)

    words = []
    begin = 0
    inquote = False
    query += ' '
    for i, c in enumerate(query):
        if c == '"':
            if not inquote:
                inquote = True
                begin = i+1
            else:
                inquote = False
        elif c == ' ' and not inquote:
            if i > 1 and query[i-1] == '"':
                word = query[begin:i-1]
            else:
                word = query[begin:i]
            words.append(word)
            begin = i +1



    mode = None
    skipwords = 0
    endclause = None

    edit = {'editform':'direct', 'targets':[], 'actor': {}, 'assignments': {}, 'datetime': datetime.datetime.now() }

    for i, word in enumerate(words):
        if skipwords:
            skipwords = skipwords - 1
            continue

        if not (endclause is None) and i >= endclause:
            endclause = None


        if word in ['IN','ADD','EDIT','DELETE','WITH','FOR','AS']:
            #end mode
            if mode is None:
                if word != "IN":
                    raise FQLParseError("FQL Query must start with IN statement!")

            #start new mode
            mode = word
            if word == "AS":
                endclause = None
                for j in range(i+1,len(words) -1):
                    if words[j] == ',':
                        endclause = j - 1
                #if endclause is None:
                #    raise FQLParseError("AS clause must end in comma, detached from any words, none found")

                if words[i+1] == 'CORRECTION':
                    edit['editform'] = 'correction'
                    if words[i+2] != "OF":
                        raise FQLParseError("Expected AS CORRECTION OF $set")
                    edit['correctionset'] = words[i+3]
                    skipwords = 3
                    if words[i+4] == 'WITH':
                        skipwords = 4
                        correction_assignments,extraskipwords = parseassignments(words, i+5)
                        if 'class' in correction_assignments:
                            edit['correctionclass'] = correction_assignments['class']
                        skipwords += extraskipwords
                elif words[i+1] == 'ALTERNATIVE':
                    edit['editform'] = 'alternative'
                    skipwords = 1
                else:
                    raise FQLParseError("Expected CORRECTION/ALTERNATIVE after AS")
            elif mode in [ 'ADD','EDIT','DELETE']:
                edit['action'] = mode.lower()


                actor_annotationtype, actor_set, actor_id, skipwords = parseactor(words,i)
                if actor_annotationtype:
                    edit['actor']['type'] = actor_annotationtype
                if actor_set:
                    edit['actor']['set'] = actor_set
                if actor_id:
                    edit['actor']['id'] = actor_id

                if mode == 'ADD':
                    edit['editform'] = "new"
                elif mode == 'DELETE':
                    if edit['actor']['type'] is folia.TextContent:
                        edit['assignments']['text'] = "" #empty text for deletion of entire structural element!
                    else:
                        edit['assignments']['class'] = "" #empty class for deletion of annotation
            elif mode == 'WITH':
                assignments,skipwords = parseassignments(words, i+1)
                for key, value in assignments.items():
                    edit['assignments'][key] = value

        elif mode == 'IN':
            try:
                namespace, docid = word.split('/',1)
            except:
                raise FQLParseError("Expected \"namespace/documentID\" after IN statement")
            if not (namespace,docid) in data:
                data[(namespace, docid)] = []
        elif mode == 'FOR':
            edit['targets'].append(word)
        else:
            raise FQLParseError("Expected statement, got " + word)


    if len(data) == 0:
        raise FQLParseError("No documents specified in IN statement!")
    if not 'action' in edit or not edit['action']:
        raise FQLParseError("Expected action statement EDIT, ADD or DELETE")
    if not 'targets' in edit or len(edit['targets']) == 0:
        if not 'id' in edit['actor']:
            raise FQLParseError("No targets found (no FOR statement?), but no ID was provided either on the actor")
    if not 'type' in edit['actor'] or not edit['actor']['type']:
        raise FQLParseError("Expected action statement EDIT, ADD or DELETE")

    log("  Parse result: " + repr(edit))

    for namespace, doc in data:
        data[(namespace,doc)].append(edit)

    return data





def doannotation(doc, data):
    response = {'returnelementids': []}
    log("Received " + str(len(data['edits'])) + " edits")


    for edit in data['edits']:
        if 'targets' in edit and edit['targets']:
            ElementClass =  doc[edit['targets'][0]].__class__ #folia.XML2CLASS[doc[data['elementid']].XMLTAG]
        else:
            ElementClass = folia.Word #default to word


        assert 'action' in edit
        assert 'targets' in edit
        assert 'actor' in edit
        assert 'assignments' in edit
        assert 'type' in edit['actor']
        Class = folia.XML2CLASS[edit['actor']['type']]
        annotationtype = Class.ANNOTATIONTYPE
        annotation = None


        if not 'editform' in edit:
            edit['editform'] = 'direct'



        if len(edit['targets']) > 1:
            commonancestors = None
            for targetid in edit['targets']:
                try:
                    target = doc[targetid]
                except:
                    response['error'] = "Target element " + targetid + " does not exist!"
                    return response
                ancestors = list( ( x.id for x in target.ancestors() if isinstance(x,folia.AbstractStructureElement) and x.id ) )
                if commonancestors is None:
                    commonancestors = copy(ancestors)
                else:
                    removeancestors = []
                    for a in commonancestors:
                        if not (a in ancestors):
                            removeancestors.append(a)
                    for a in removeancestors:
                        commonancestors.remove(a)
            if commonancestors:
                commonancestor = commonancestors[0]
                log("Common ancestor as return element: "+ commonancestor )
                if not commonancestor in response['returnelementids']:
                    response['returnelementids'].append(commonancestor)
        elif edit['targets']:
            for targetid in edit['targets']:
                try:
                    target = doc[targetid]
                    if not target.id in response['returnelementids']:
                        response['returnelementids'].append(target.id)
                except:
                    response['error'] = "Target element " + targetid + " does not exist!"
                    return response
            commonancestor = target.ancestor(folia.AbstractStructureElement).id
        else:
            commonancestor = doc.data[0]





        if not 'set' in edit['actor'] or edit['actor']['set'] == 'null':
            if 'id' in edit['actor'] and edit['actor']['id']:
                edit['actor']['set'] = doc[edit['actor']['id']].set
            else:
                #see declaration
                found = 0
                for annotationtype2, set2 in doc.annotations:
                    if annotationtype2 == annotationtype:
                        found += 1
                        edit['actor']['set'] = set2
                        if found > 1:
                            response['error'] = "No set was specified for type " + edit['actor']['type'] + ", but the document has multiple ambiguous options, unable to resolve automatically"
                            return response
                if not found:
                    edit['actor']['set'] = 'undefined'

        if edit['actor']['set'] != 'undefined':
            #declare if undeclared
            if not doc.declared(annotationtype,edit['actor']['set']):
                doc.declare(annotationtype, edit['actor']['set'])

        log("Processing edit: "+ str(repr(edit)) )
        log("Class=" + Class.__name__ )

        if edit['action'] == 'delete' and 'id' in edit['actor']:
            #delete the element with the specified ID
            target = doc[edit['actor']['id']]
            if edit['editform'] == 'direct':
                response['log'] = "Deletion of " + target.id + ", by " + data['annotator']
            elif edit['editform'] == 'correction':
                response['log'] = "Deletion of " + target.id + " '" + target.text() + "' (correction " + edit['correctionclass']+"), by " + data['annotator']
            else:
                raise Exception("Invalid editform for DELETE" + edit['editform'])
            log(response['log'])

            #undo any space=False prior to our deleted entry
            try:
                index = target.parent.data.index(target)
            except ValueError:
                index = 0
            if index > 0 and isinstance(target.parent.data[index-1], folia.Word) and not target.parent.data[index-1].space:
                target.parent.data[index-1].space = True

            if edit['editform'] == 'direct':
                #we have a text deletion!
                p = target.parent
                p.remove(target)
                if not p.id in response['returnelementids']:
                    response['returnelementids'].append( p.id )
            elif edit['editform'] == 'correction':
                #we have a deletion as a correction!
                if Class is folia.Word:
                    if isinstance(target, folia.Word):
                        p = target.ancestor(folia.AbstractStructureElement)
                        p.deleteword(target,set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime']) #does correction
                else:
                    #probably a span annotation
                    p = target.parent
                    p.correct(original=target, set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'])

                if not p.id in response['returnelementids']:
                    response['returnelementids'].append(p.id )

        elif issubclass(Class, folia.TextContent): ################### EDIT OF TEXT CONTENT #######################################
            #Text Content, each target will get a copy
            if len(edit['targets']) > 1 and 'merge' in edit['assignments'] and edit['assignments']['merge']:
                #merge elements

                log("Text edit of multiple elements")
                targets = []
                ancestor = None
                for target in edit['targets']:
                    targets.append( doc[target] )
                    if not ancestor:
                        ancestor = doc[target].ancestor(folia.AbstractStructureElement)
                        index = ancestor.data.index(doc[target]) #TODO, can't find if already part of a correction
                    elif ancestor != doc[target].ancestor(folia.AbstractStructureElement):
                        response['error'] = "Unable to merge words, they are not in the same structure element"
                        return response

                if edit['editform'] in ('direct','new'):
                    #remove all targets and insert a new one in its place
                    response['log'] = "Merging/replacing words, by " + data['annotator']
                    log(response['log'])
                    for target in targets:
                        ancestor.remove(target)
                    ancestor.insert(index, ElementClass(doc, folia.TextContent(doc, edit['assignments']['text'], set=edit['actor']['set']), generate_id_in=ancestor ) )
                elif edit['editform'] == 'correction':
                    response['log'] = "Merging/replacing words (correction " + edit['correctionclass'] + "), by " + data['annotator']
                    log(response['log'])
                    ancestor.mergewords(ElementClass(doc, folia.TextContent(doc, edit['assignments']['text'], set=edit['actor']['set']), generate_id_in=ancestor), *targets, set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'] )
                elif edit['editform'] == 'alternative':
                    raise NotImplementedError


                if not ancestor.id in response['returnelementids']:
                    response['returnelementids'].append( ancestor.id )
            else:

                for targetid in edit['targets']:
                    try:
                        target = doc[targetid]
                    except:
                        response['error'] = "Target element " + targetid + " does not exist!"
                        return response

                    log("Text edit of " + target.id)

                    if edit['editform'] in ('direct','new'):
                        if 'insertright' in edit['assignments']:
                            response['log'] = "Right insertion after " + target.id + ", by " + data['annotator']
                            log(response['log'])

                            #Undo any space=False attribute on the word we insert after, if set
                            if isinstance(target, folia.Word) and not target.space:
                                target.space = True

                            try:
                                index = target.parent.data.index(target) + 1
                            except ValueError:
                                response['error'] = "Unable to find insertion index"
                                return response

                            for wordtext in reversed(edit['assignments']['insertright'].split(' ')):
                                target.parent.insert(index, ElementClass(doc, wordtext,generate_id_in=target.parent ) )
                            if not target.parent.id in response['returnelementids']:
                                response['returnelementids'].append( target.parent.id )
                        elif 'insertleft' in edit['assignments']:
                            response['log'] = "Left insertion before " + target.id + ", by " + data['annotator']
                            log(response['log'])
                            try:
                                index = target.parent.data.index(target)
                            except ValueError:
                                response['error'] = "Unable to find insertion index"
                                return response

                            #undo any space=False prior to our insertion point
                            if index > 0 and target.parent.data[index-1] and isinstance(target.parent.data[index-1], folia.Word) and not target.parent.data[index-1].space:
                                target.parent.data[index-1].space = True

                            for wordtext in reversed(edit['assignments']['insertleft'].split(' ')):
                                target.parent.insert(index, ElementClass(doc, wordtext,generate_id_in=target.parent ) )
                            if not target.parent.id in response['returnelementids']:
                                response['returnelementids'].append( target.parent.id )

                        elif 'split' in edit['assignments']:
                            response['log'] = "Split of " + target.id + ", by " + data['annotator']
                            log(response['log'])

                            p = target.parent

                            try:
                                index = p.data.index(target)
                            except ValueError:
                                response['error'] = "Unable to find insertion index"
                                return response

                            p.remove(p.data[index])

                            for wordtext in reversed(edit['assignments']['text'].split(' ')):
                                p.insert(index, ElementClass(doc, folia.TextContent(doc, value=wordtext ), generate_id_in=p ) )

                            if not p.id in response['returnelementids']:
                                response['returnelementids'].append( p.id )
                        elif 'text' in edit['assignments'] and edit['assignments']['text']:
                            response['log'] = "Text content change of " + target.id + " (" + edit['assignments']['text']+"), by " + data['annotator']
                            if not 'class' in edit['assignments']:
                                edit['assignments']['class'] = 'current'
                            log(response['log'])
                            if not 'set' in edit['assignments']: edit['assignments']['set'] = edit['actor']['set']
                            target.replace(Class,value=edit['assignments']['text'], set=edit['assignments']['set'], cls=edit['assignments']['class'],annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime']) #does append if no replacable found
                        else:
                            response['log'] = "Text deletion of " + target.id + ", by " + data['annotator']
                            log(response['log'])

                            #TODO: remove text element only
                            raise NotImplementedError("Text deletion not implemented")



                    elif edit['editform'] == 'alternative':
                        response['error'] = "Can not add alternative text yet, not implemented"
                        return response
                    elif edit['editform'] == 'correction':
                        if 'insertright' in edit['assignments']:
                            #Undo any space=False attribute if set
                            if isinstance(target, folia.Word) and not target.space:
                                target.space = True

                            response['log'] = "Right insertion '" + edit['assignments']['insertright'] + "' (correction " + edit['correctionclass'] + ") after " + target.id +", by " + data['annotator']
                            log(response['log'])
                            newwords = []
                            for wordtext in edit['assignments']['insertright'].split(' '):
                                if not 'set' in edit['assignments']: edit['assignments']['set'] = edit['actor']['set']
                                newwords.append( ElementClass(doc, folia.TextContent(doc, wordtext, set=edit['assignments']['set']), generate_id_in=target.parent ) )
                            target.parent.insertword(newwords, target, set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'] )
                            if not target.parent.id in response['returnelementids']:
                                response['returnelementids'].append(target.parent.id )
                        elif 'insertleft' in edit['assignments']:

                            response['log'] = "Left insertion '" + edit['assignments']['insertleft'] + "' (correction " + edit['correctionclass'] + ") before " + target.id + ", by " + data['annotator']
                            log(response['log'])

                            #undo any space=False prior to our insertion point
                            try:
                                index = target.parent.data.index(target)
                            except ValueError:
                                index = 0
                            if index > 0 and  isinstance(target.parent.data[index-1], folia.Word) and not target.parent.data[index-1].space:
                                target.parent.data[index-1].space = True

                            newwords = []
                            if not 'set' in edit['assignments']: edit['assignments']['set'] = edit['actor']['set']
                            for wordtext in edit['assignments']['insertleft'].split(' '):
                                newwords.append( ElementClass(doc, folia.TextContent(doc, wordtext, set=edit['assignments']['set']), generate_id_in=target.parent ) )
                            target.parent.insertwordleft(newwords, target, set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'] )
                            if not target.parent.id in response['returnelementids']:
                                response['returnelementids'].append(target.parent.id )
                        elif 'split' in edit['assignments']:
                            response['log'] = "Split of " + target.id + " '"+ edit['assignments']['text'] +"' (correction " + edit['correctionclass']+"), by " + data['annotator']
                            log(response['log'])
                            newwords = []
                            if not 'set' in edit['assignments']: edit['assignments']['set'] = edit['actor']['set']
                            for wordtext in edit['assignments']['text'].split(' '):
                                newwords.append( ElementClass(doc, folia.TextContent(doc, wordtext, set=edit['assignments']['set']), generate_id_in=target.parent ) )
                            target.parent.splitword(target, *newwords, set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'] )
                            if not target.parent.id in response['returnelementids']:
                                response['returnelementids'].append(target.parent.id )
                        elif edit['assignments']['text']:
                            response['log'] = "Text correction '" + edit['assignments']['text'] + "' on " + target.id + " (correction " + edit['correctionclass']+"), by " + data['annotator']
                            log(response['log'])
                            if not 'class' in edit['assignments']:
                                edit['assignments']['class'] = 'current'
                            target.correct(new=folia.TextContent(doc, value=edit['assignments']['text'], cls=edit['assignments']['class'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'] ), set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'])
                        else:
                            response['log'] = "Deletion of " + target.id + " '" + target.text() + "' (correction " + edit['correctionclass']+"), by " + data['annotator']
                            log(response['log'])

                            raise NotImplementedError #delete text only, as correction

        elif issubclass(Class, folia.AbstractTokenAnnotation): ################### EDIT OF TOKEN ANNOTATION #######################################

            log("Edit of token annotation")
            #Token annotation, each target will get a copy (usually just one target)
            for targetid in edit['targets']:
                try:
                    target = doc[targetid]
                except:
                    response['error'] = "Target element " + targetid + " does not exist!"
                    return response

                if not 'set' in edit['assignments']: edit['assignments']['set'] = edit['actor']['set']

                if edit['editform'] == 'new' and edit['assignments']['class']:
                    response['log'] = "Add of " + Class.__name__ + " (" + edit['assignments']['class'] + ") in " + target.id + ", by " + data['annotator']
                    log(response['log'])
                    target.append(Class,set=edit['assignments']['set'], cls=edit['assignments']['class'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'])
                elif edit['editform'] == 'direct':
                    if edit['assignments']['class']:
                        response['log'] = "Edit of " + Class.__name__ + " (" + edit['assignments']['class'] + ") in " + target.id + ", by " + data['annotator']
                        log(response['log'])
                        target.replace(Class,set=edit['assignments']['set'], cls=edit['assignments']['class'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime']) #does append if no replacable found
                    else:
                        response['log'] = "Deletion of " + Class.__name__ + " in " + target.id + ", by " + data['annotator']
                        log(response['log'])
                        #we have a deletion
                        replace = Class.findreplaceables(target, edit['assignments']['set'])
                        if len(replace) == 1:
                            #good
                            target.remove(replace[0])
                        elif len(replace) > 1:
                            response['error'] = "Unable to delete, multiple ambiguous candidates found!"
                            return response
                elif edit['editform'] == 'alternative':
                    response['log'] = "Adding alternative of " + Class.__name__ + " (" + edit['assignments']['class'] + ") in " + target.id + ", by " + data['annotator']
                    log(response['log'])
                    target.append(Class,set=edit['assignments']['set'], cls=edit['assignments']['class'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'], alternative=True)
                elif edit['editform'] == 'correction':
                    if edit['assignments']['class']:
                        response['log'] = "Correcting " + Class.__name__ + " (" + edit['assignments']['class'] + ") in " + target.id + ", by " + data['annotator']
                        log(response['log'])
                        log("Calling correct")
                        target.correct(new=Class(doc, set=edit['assignments']['set'], cls=edit['assignments']['class'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime']), set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'])
                    else:
                        response['log'] = "Deletion of " + Class.__name__ + " (" + edit['assignments']['class'] + "), as correction, in " + target.id + ", by " + data['annotator']
                        log(response['log'])
                        log("Calling correct")
                        target.correct(original=target.annotation(Class, edit['actor']['set']), set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'])



        elif issubclass(Class, folia.AbstractSpanAnnotation): ################### EDIT OF SPAN ANNOTATION #######################################

            log("Edit of span annotation")

            targets = []
            for targetid in edit['targets']:
                try:
                    targets.append( doc[targetid] )
                except:
                    response['error'] = "Target element " + targetid + " does not exist!"
                    return response

            if not 'set' in edit['assignments']: edit['assignments']['set'] = edit['actor']['set']


            if edit['editform'] in ('direct','new'):
                #Span annotation, one annotation spanning all tokens
                if edit['editform'] == 'new':
                    #this is a new span annotation

                    response['log'] = "Adding " + Class.__name__ + " (" + edit['assignments']['class'] + ") for " + ",".join([x.id for x in targets]) + "; by " + data['annotator']
                    log(response['log'])

                    #create layer on common ancestor of all targets, use existing layer if possible, make new layer if not
                    layers = doc[commonancestor].layers(annotationtype, edit['assignments']['set'])
                    if len(layers) >= 1:
                        layer = layers[0]
                    else:
                        layer = doc[commonancestor].append(folia.ANNOTATIONTYPE2LAYERCLASS[annotationtype])

                    layer.append(Class, *targets, set=edit['assignments']['set'], cls=edit['assignments']['class'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'], generate_id_in=doc[commonancestor])

                    if not layer.ancestor(folia.AbstractStructureElement).id in response['returnelementids']:
                        response['returnelementids'].append(layer.ancestor(folia.AbstractStructureElement).id)


                elif 'id' in edit['actor']:
                    if edit['assignments']['class']:
                        response['log'] = "Editing span annotation " + Class.__name__ + " (" + edit['assignments']['class'] + ") for " + ",".join([x.id for x in targets]) + "; by " + data['annotator']
                    else:
                        response['log'] = "Deleting span annotation " + Class.__name__ + " for " + ",".join([x.id for x in targets]) + "; by " + data['annotator']
                    log(response['log'])
                    #existing span annotation, we should have an ID
                    try:
                        annotation = doc[edit['actor']['id']]
                    except:
                        response['error'] = "No existing span annotation with id " + edit['actor']['id'] + " found"
                        return response

                    currenttargets = annotation.wrefs()
                    if currenttargets != targets:
                        if annotation.hasannotation(Class):
                            response['error'] = "Unable to change the span of this annotation as there are nested span annotations embedded"
                            return response
                        else:
                            annotation.data = targets




                    if edit['assignments']['class']:
                        annotation.cls = edit['assignments']['class']
                        annotation.annotator = data['annotator']
                        annotation.annotatortype = folia.AnnotatorType.MANUAL
                    else:
                        #delete:
                        annotation.parent.remove(annotation)

                    if not annotation.ancestor(folia.AbstractStructureElement).id in response['returnelementids']:
                        response['returnelementids'].append(annotation.ancestor(folia.AbstractStructureElement).id)

                else:
                    #no ID, fail
                    response['error'] = "Unable to edit span annotation without explicit id"
                    return response

            elif edit['editform'] == 'alternative':
                response['error'] = "Only direct edit form is supported for span annotation elements at this time. Corrections and alternatives to be implemented still."
                return response
            elif edit['editform'] == 'correction':
                if 'id' in edit['actor']:
                    if edit['assignments']['class']:
                        response['log'] = "Editing span annotation " + Class.__name__ + " (" + edit['assignments']['class'] + ", correction " + edit['correctionclass']+") for " + ",".join([x.id for x in targets]) + "; by " + data['annotator']
                    else:
                        response['log'] = "Deleting span annotation " + Class.__name__ + " (correction " + edit['correctionclass']+") for " + ",".join([x.id for x in targets]) + "; by " + data['annotator']
                    log(response['log'])

                    #existing span annotation, we should have an ID
                    try:
                        annotation = doc[edit['actor']['id']]
                    except:
                        response['error'] = "No existing span annotation with id " + edit['actor']['id'] + " found"
                        return response

                    layer = annotation.parent #may turn out to be another SpanAnnotation or Correction instead! should be okay

                    currenttargets = annotation.wrefs()
                    if currenttargets != targets:
                        if annotation.hasannotation(Class):
                            response['error'] = "Unable to change the span of this annotation as there are nested span annotations embedded"
                            return response

                    if edit['assignments']['class']:
                        #TODO: will require extra work for dependencies and coref chains (handling of subelements)
                        newannotation = Class(doc, *targets, cls=edit['assignments']['class'], set=edit['assignments']['set'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'], generate_id_in=layer.parent )
                        layer.correct(original=annotation,new=newannotation, set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'])
                    else:
                        #delete
                        layer.correct(original=annotation,set=edit['correctionset'], cls=edit['correctionclass'], annotator=data['annotator'], annotatortype=folia.AnnotatorType.MANUAL, datetime=edit['datetime'])

                    if not annotation.ancestor(folia.AbstractStructureElement).id in response['returnelementids']:
                        response['returnelementids'].append( annotation.ancestor(folia.AbstractStructureElement).id)

                else:
                    #no ID, fail
                    response['error'] = "Unable to edit span annotation without explicit id"
                    return response
        else:
            response['error'] = "Unable to edit annotations of type " + Class.__name__
            return response

    log("Return elements: " + repr(response['returnelementids']))
    if response['returnelementids']:
        #log(doc[response['returnelementid']].xmlstring())
        return response
    else:
        del response['returnelementids']
        return response




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

            annotationdata = { 'edits': data[(ns,docid)], 'annotator': request['annotator'] }
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
