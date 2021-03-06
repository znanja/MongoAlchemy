# The MIT License
# 
# Copyright (c) 2010 Jeffrey Jenkins
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
'''

A `mongoalchemy` document is used to define a mapping between a python object 
and a document in a Mongo Database.  Mappings are defined by creating a 
subclass of :class:`Document` with attributes to define 
what maps to what.  The two main types of attributes are :class:`~mongoalchemy.fields.Field` 
and :class:`Index`

A :class:`~mongoalchemy.fields.Field` is used to define the type of a field in
mongo document, any constraints on the values, and to provide methods for 
transforming a value from a python object into something Mongo understands and 
vice-versa.

A :class:`~Index` is used to define an index on the underlying collection 
programmatically.  A document can have multiple indexes by adding extra 
:class:`~Index` attributes


'''
import pymongo
from pymongo import GEO2D
from collections import defaultdict
from mongoalchemy.util import classproperty, UNSET
from mongoalchemy.query_expression import QueryField
from mongoalchemy.fields import ObjectIdField, Field, BadValueException, SCALAR_MODIFIERS
from mongoalchemy.exceptions import DocumentException, MissingValueException, ExtraValueException, FieldNotRetrieved, BadFieldSpecification

document_type_registry = defaultdict(dict)

class DocumentMeta(type):
    def __new__(mcs, classname, bases, class_dict):
        # Validate Config Options
        # print '-' * 20, classname, '-' * 20
        
        # Create Class
        class_dict['_subclasses'] = {}
        new_class = type.__new__(mcs, classname, bases, class_dict)
        
        if new_class.config_extra_fields not in ['error', 'ignore']:
            raise DocumentException("config_extra_fields must be one of: 'error', 'ignore'")
        
        # 1. Set up links between fields and the document class
        new_id = False
        for name, value in class_dict.iteritems():
            if not isinstance(value, Field):
                continue
            if value.is_id and name != 'mongo_id':
                new_id = True
            value._set_name(name)
            value._set_parent(new_class)
        
        if new_id:
            new_class.mongo_id = None
        
        # 2. create a dict of fields to set on the object
        new_class._fields = {}
        for b in bases:
            # print b
            if not hasattr(b, 'get_fields'):
                continue
            for name, field in b.get_fields().iteritems():    
                new_class._fields[name] = field

        for name, maybefield in class_dict.iteritems():
            if not isinstance(maybefield, Field):
                continue
            new_class._fields[name] = maybefield
        
        # 3. register type
        if new_class.config_namespace != None:
            name = new_class.config_full_name
            if name == None:
                name = new_class.__name__
            document_type_registry[new_class.config_namespace][name] = new_class

        # 5. Add proxies
        for name, field in new_class.get_fields().iteritems():
            if field.proxy is not None:
                setattr(new_class, field.proxy, Proxy(name))
            if field.iproxy is not None:
                setattr(new_class, field.iproxy, IProxy(name))
        
        # 4.  Add subclasses

        for b in bases:
            if 'Document' in globals() and issubclass(b, Document):
                b.add_subclass(new_class)
            if not hasattr(b, 'config_polymorphic_collection'):
                continue
            if b.config_polymorphic_collection and 'config_collection' not in class_dict:
                new_class.config_collection_name = b.get_collection_name()


        return new_class

class Document(object):
    __metaclass__ = DocumentMeta
    
    mongo_id = ObjectIdField(required=False, db_field='_id', on_update='ignore')
    ''' Default field for the mongo object ID (``_id`` in the database). This field
        is automatically set on objects when they are saved into the database.
        This field can be overridden in subclasses if the default ID is not
        acceptable '''
    
    config_namespace = 'global'
    ''' The namespace is used to determine how string class names should be 
        looked up.  If an instance of DocumentField is created using a string,
        it will be looked up using the value of this variable and the string.
        To have more than one namespace create a subclass of Document
        overriding this class variable.  To turn off caching all together, 
        create a subclass where namespace is set to None.  Doing this will 
        disable using strings to look up document names, which will make 
        creating self-referencing documents impossible.  The default value is
        "global"
    '''
    
    config_polymorphic = None
    ''' The variable to use when determining which class to instantiate a
        database object with.  It is the name of an attribute which
        will be used to decide the type of the object.  If you want more 
        control over which class is selected, you can override 
        ``get_subclass``.
    '''
    
    config_polymorphic_collection = False
    ''' Use the base class collection name for the subclasses.  Default: False
    '''


    config_polymorphic_identity = None
    ''' When using a string value with ``config_polymorphic_on`` in a parent 
        class, this is the value that the attribute is compared to when 
        determining 
    '''

    config_full_name = None
    ''' If namespaces are being used, the key for a class is normally
        the class name.  In some cases the same class name may be used in 
        different modules.  This field allows a longer unambiguous name
        to be given.  It may also be used in error messages or string 
        representations of the class
    '''
    
    config_extra_fields = 'error'
    ''' Controls the method to use when dealing with fields passed in to the
        document constructor.  Possible values are 'error' and 'ignore'. Any 
        fields which couldn't be mapped can be retrieved (and edited) using
        :func:`~Document.get_extra_fields` '''

    def __init__(self, retrieved_fields=None, loading_from_db=False, **kwargs):
        ''' :param retrieved_fields: The names of the fields returned when loading \
                a partial object.  This argument should not be explicitly set \
                by subclasses
            :param \*\*kwargs:  The values for all of the fields in the document. \
                Any additional fields will raise a :class:`~mongoalchemy.document.ExtraValueException` and \ 
                any missing (but required) fields will raise a :class:`~mongoalchemy.document.MissingValueException`. \
                Both types of exceptions are subclasses of :class:`~mongoalchemy.document.DocumentException`.
        '''
        self.partial = retrieved_fields is not None
        self.retrieved_fields = self.__normalize(retrieved_fields)
        
        self._dirty = {}
        
        self._field_values = {}
        self.__extra_fields = {}
        
        cls = self.__class__
                
        fields = self.get_fields()
        for name, field in fields.iteritems():
            if self.partial and field.db_field not in self.retrieved_fields:
                continue
            
            if name in kwargs:
                getattr(cls, name).set_value(self, kwargs[name], from_db=loading_from_db)
                continue
            # DO I NEED THIS?
            # elif field.default != UNSET:
            #     getattr(cls, name).set_value(self, field.default, from_db=loading_from_db)

        
        for k in kwargs:
            if k not in fields:
                if self.config_extra_fields == 'ignore':
                    self.__extra_fields[k] = kwargs[k]
                else:
                    raise ExtraValueException(k)

        self.__extra_fields_orig = dict(self.__extra_fields)
    
    def __deepcopy__(self, memo):
        return type(self).unwrap(self.wrap(), session=self._get_session())

    @classmethod
    def add_subclass(cls, subclass):
        ''' Register a subclass of this class.  Maps the subclass to the 
            value of subclass.config_polymorphic_identity if available.
        '''
        # if not polymorphic, stop
        if hasattr(subclass, 'config_polymorphic_identity'):
            attr = subclass.config_polymorphic_identity
            cls._subclasses[attr] = subclass


    @classmethod
    def get_subclass(cls, obj):
        ''' Get the subclass to use when instantiating a polymorphic object.
            The default implementation looks at ``cls.config_polymorphic``
            to get the name of an attribute.  Subclasses automatically 
            register their value for that attribute on creation via their
            ``config_polymorphic_identity`` field.  This process is then 
            repeated recursively until None is returned (indicating that the
            current class is the correct one)

            This method can be overridden to allow any method you would like 
            to use to select subclasses. It should return either the subclass
            to use or None, if the original class should be used.
        '''
        if cls.config_polymorphic is None:
            return

        value = obj.get(cls.config_polymorphic)
        value = cls._subclasses.get(value)
        if value == cls or value is None:
            return None

        sub_value = value.get_subclass(obj)
        if sub_value is None:
            return value
        return sub_value
    def __eq__(self, other):
        try:
            self.mongo_id == other.mongo_id
        except:
            return False

    def get_dirty_ops(self, with_required=False):
        ''' Returns a dict with the update operations necessary to make the 
            changes to this object to the database version.  It is mainly used
            internally for :func:`~mongoalchemy.session.Session.update` but
            may be useful for diagnostic purposes as well.
            
            :param with_required: Also include any field which is required.  This \
                is useful if the method is being called for the purposes of \
                an upsert where all required fields must always be sent.
        '''
        update_expression = {}
        for name, field in self.get_fields().iteritems():
            if field.db_field == '_id':
                continue
            dirty_ops = field.dirty_ops(self)
            if not dirty_ops and with_required and field.required:
                dirty_ops = field.update_ops(self)
                if not dirty_ops:
                    raise MissingValueException(name)
            
            for op, values in dirty_ops.iteritems():
                update_expression.setdefault(op, {})
                for key, value in values.iteritems():
                    update_expression[op][key] = value

        if self.config_extra_fields == 'ignore':
            old_extrakeys = set(self.__extra_fields_orig.keys())
            cur_extrakeys = set(self.__extra_fields.keys())

            new_extrakeys = cur_extrakeys - old_extrakeys
            rem_extrakeys = old_extrakeys - cur_extrakeys
            same_extrakeys = cur_extrakeys & old_extrakeys

            update_expression.setdefault('$unset', {})
            for key in rem_extrakeys:
                update_expression['$unset'][key] = True

            update_expression.setdefault('$set', {})
            for key in new_extrakeys:
                update_expression['$set'][key] = self.__extra_fields[key]

            for key in same_extrakeys:
                if self.__extra_fields[key] != self.__extra_fields_orig[key]:
                    update_expression['$set'][key] = self.__extra_fields[key]

        return update_expression
    
    def get_extra_fields(self):
        ''' if :attr:`Document.config_extra_fields` is set to 'ignore', this method will return
            a dictionary of the fields which couldn't be mapped to the document.
        '''
        return self.__extra_fields
    
    @classmethod
    def get_fields(cls):
        ''' Returns a dict mapping the names of the fields in a document 
            or subclass to the associated :class:`~mongoalchemy.fields.Field`
        '''
        return cls._fields
    
    @classmethod
    def class_name(cls):
        ''' Returns the name of the class. The name of the class is also the 
            default collection name.  
            
            .. seealso:: :func:`~Document.get_collection_name`
        '''
        return cls.__name__
    
    @classmethod
    def get_collection_name(cls):
        ''' Returns the collection name used by the class.  If the ``config_collection_name``
            attribute is set it is used, otherwise the name of the class is used.'''
        if not hasattr(cls, 'config_collection_name'):
            return cls.__name__
        return cls.config_collection_name
    
    @classmethod
    def get_indexes(cls):
        ''' Returns all of the :class:`~mongoalchemy.document.Index` instances
            for the current class.'''
        ret = []
        for name in dir(cls):
            field = getattr(cls, name)
            if isinstance(field, Index):
                ret.append(field)
        return ret
    
    @classmethod
    def __normalize(cls, fields):
        if not fields:
            return fields
        ret = {}
        for f in fields:
            strf = str(f)
            if '.' in strf:
                first, _, second = strf.partition('.')
                ret.setdefault(first, []).append(second)
            else:
                ret[strf] = None
        return ret
    
    def has_id(self):
        try:
            getattr(self, 'mongo_id')
        except AttributeError:
            return False
        return True 
    
    def commit(self, db, safe=True):
        ''' Save this object to the database and set the ``_id`` field of this
            document to the returned id.
            
            :param db: The pymongo database to write to
        '''
        collection = db[self.get_collection_name()]
        for index in self.get_indexes():
            index.ensure(collection)
        id = collection.save(self.wrap(), safe=safe)
        self.mongo_id = id
        
    def wrap(self):
        ''' Returns a transformation of this document into a form suitable to 
            be saved into a mongo database.  This is done by using the ``wrap()``
            methods of the underlying fields to set values.'''
        res = {}
        for k, v in self.__extra_fields.iteritems():
            res[k] = v
        cls = self.__class__
        for name in self.get_fields():
            field = getattr(cls, name)
            # if not isinstance(field, QueryField):
            #     continue
            try:
                value = getattr(self, name)
                res[field.db_field] = field.wrap(value)
            except AttributeError as e:
                if field.required:
                    raise MissingValueException(name)
        return res
    
    @classmethod
    def validate_unwrap(cls, obj, fields=None, session=None):
        ''' Attempts to unwrap the document, and raises a BadValueException if
            the operation fails. A TODO is to make this function do the checks
            without actually doing the (potentially expensive) 
            deserialization'''
        try:
            cls.unwrap(obj, fields=fields, session=session)
        except Exception as e:
            e_type = type(e).__name__
            cls_name = cls.__name__
            raise BadValueException(cls_name, obj, '%s Exception validating document' % e_type, cause=e)
    
    @classmethod
    def unwrap(cls, obj, fields=None, session=None):
        ''' Returns an instance of this document class based on the mongo object 
            ``obj``.  This is done by using the ``unwrap()`` methods of the 
            underlying fields to set values.
            
            :param obj: a ``SON`` object returned from a mongo database
            :param fields: A list of :class:`mongoalchemy.query.QueryField` objects \
                    for the fields to load.  If ``None`` is passed all fields  \
                    are loaded
            '''

        subclass = cls.get_subclass(obj)
        if subclass and subclass != cls:
            unwrapped = subclass.unwrap(obj, fields=fields, session=session)
            unwrapped._session = session
            return unwrapped
        # Get reverse name mapping
        name_reverse = {}
        for name, field in cls.get_fields().iteritems():
            name_reverse[field.db_field] = name
        # Unwrap
        params = {}
        for k, v in obj.iteritems():
            k = name_reverse.get(k, k)
            if not hasattr(cls, k) and cls.config_extra_fields:
                params[str(k)] = v
                continue

            field = getattr(cls, k)
            field_is_doc = fields is not None and isinstance(field.get_type(), DocumentField)

            extra_unwrap = {}
            if field.has_autoload:
                extra_unwrap['session'] = session
            if field_is_doc:
                normalized_fields = cls.__normalize(fields)
                unwrapped = field.unwrap(v, fields=normalized_fields.get(k), **extra_unwrap)
            else:
                unwrapped = field.unwrap(v, **extra_unwrap)
            unwrapped = field.localize(session, unwrapped)
            params[str(k)] = unwrapped
        
        if fields is not None:
            params['retrieved_fields'] = fields
        obj = cls(loading_from_db=True, **params)
        obj.__mark_clean()
        obj._session = session
        return obj

    _session = None
    def _get_session(self):
        return self._session
    def _set_session(self, session):
        self._session = session

    def __mark_clean(self):
        self._dirty.clear()
        

class DictDoc(object):
    ''' Adds a mapping interface to a document.  Supports ``__getitem__`` and 
        ``__contains__``.  Both methods will only retrieve values assigned to
        a field, not methods or other attributes.
    '''
    def __getitem__(self, name):
        ''' Gets the field ``name`` from the document '''
        fields = self.get_fields()
        if name in fields:
            return getattr(self, name)
        raise KeyError(name)
    
    def __setitem__(self, name, value):
        ''' Sets the field ``name`` on the document '''
        setattr(self, name, value)
    
    def setdefault(self, name, value):
        ''' if the ``name`` is set, return its value.  Otherwse set ``name`` to
            ``value`` and return ``value``'''
        if name in self:
            return self[name]
        self[name] = value
        return self[name]
    
    def __contains__(self, name):
        ''' Return whether a field is present.  Fails if ``name`` is not a 
            field or ``name`` is not set on the document or if ``name`` was 
            not a field retrieved from the database
        '''
        try:
            self[name]
        except FieldNotRetrieved:
            return False
        except AttributeError:
            return False
        except KeyError:
            return False
        return True
        

class DocumentField(Field):
    ''' A field which wraps a :class:`Document`'''
    
    has_subfields = True
    has_autoload = True
    
    def __init__(self, document_class, **kwargs):
        super(DocumentField, self).__init__(**kwargs)
        self.__type = document_class
    
    @property
    def type(self):
        if not isinstance(self.__type, basestring) and issubclass(self.__type, Document):
            return self.__type
        if self.parent.config_namespace == None:
            raise BadFieldSpecification('Document namespace is None.  Strings are not allowed for DocumentFields')
        type = document_type_registry[self.parent.config_namespace].get(self.__type)
        if type == None or not issubclass(type, Document):
            raise BadFieldSpecification('No type found for %s.  Maybe it has not been imported yet and is not registered?' % self.__type)
        return type
    
    def dirty_ops(self, instance):
        ''' Returns a dict of the operations needed to update this object.  
            See :func:`Document.get_dirty_ops` for more details.'''
        try:
            document = getattr(instance, self._name)
        except AttributeError:
            return {}
        if len(document._dirty) == 0 and \
           self.__type.config_extra_fields != 'ignore':
            return {}
        
        ops = document.get_dirty_ops()
        
        ret = {}
        for op, values in ops.iteritems():
            ret[op] = {}
            for key, value in values.iteritems():
                name = '%s.%s' % (self._name, key)
                ret[op][name] = value
        return ret
    
    def subfields(self):
        ''' Returns the fields that can be retrieved from the enclosed 
            document.  This function is mainly used internally'''
        return self.type.get_fields()
    
    def sub_type(self):
        return self.type
    
    def is_valid_unwrap(self, value, fields=None):
        ''' Called before wrapping.  Calls :func:`~DocumentField.is_valid_unwrap` and 
            raises a :class:`BadValueException` if validation fails            
            
            :param value: The value to validate
            :param fields: The fields being returned if this is a partial \
                document. They will be ignored when validating the fields \
                of ``value``
        '''
        try:
            self.validate_unwrap(value, fields=fields)
        except BadValueException as bve:
            return False
        return True
    
    def wrap(self, value):
        ''' Validate ``value`` and then use the document's class to wrap the 
            value'''
        self.validate_wrap(value)
        return self.type.wrap(value)
    
    def unwrap(self, value, fields=None, session=None):
        ''' Validate ``value`` and then use the document's class to unwrap the 
            value'''
        self.validate_unwrap(value, fields=fields, session=session)
        return self.type.unwrap(value, fields=fields, session=session)
    
    def validate_wrap(self, value):
        ''' Checks that ``value`` is an instance of ``DocumentField.type``.
            if it is, then validation on its fields has already been done and
            no further validation is needed.
        '''
        if not isinstance(value, self.type):
            self._fail_validation_type(value, self.type)
    
    def validate_unwrap(self, value, fields=None, session=None):
        ''' Validates every field in the underlying document type.  If ``fields`` 
            is not ``None``, only the fields in ``fields`` will be checked.
        '''
        try:
            self.type.validate_unwrap(value, fields=fields, session=session)
        except BadValueException as bve:
            self._fail_validation(value, 'Bad value for DocumentField field', cause=bve)

class BadIndexException(Exception):
    pass

class Index(object):
    ''' This class is  used in the class definition of a :class:`~Document` to 
        specify a single, possibly compound, index.  ``pymongo``'s ``ensure_index``
        will be called on each index before a database operation is executed 
        on the owner document class.
        
        **Example**
            
            >>> class Donor(Document):
            ...     name = StringField()
            ...     age = IntField(min_value=0)
            ...     blood_type = StringField()
            ...     
            ...     i_name = Index().ascending('name')
            ...     type_age = Index().ascending('blood_type').descending('age')
    '''
    ASCENDING = pymongo.ASCENDING
    DESCENDING = pymongo.DESCENDING
    
    def __init__(self):
        self.components = []
        self.__unique = False
        self.__drop_dups = False
        
        self.__min = None
        self.__max = None
        self.__bucket_size = None

    
    def ascending(self, name):
        ''' Add a descending index for ``name`` to this index.
        
            :param name: Name to be used in the index
        '''
        self.components.append((name, Index.ASCENDING))
        return self

    def descending(self, name):
        ''' Add a descending index for ``name`` to this index.
            
            :param name: Name to be used in the index
        '''
        self.components.append((name, Index.DESCENDING))
        return self
    
    def geo2d(self, name, min=None, max=None):
        """ Create a 2d index.  See: 
            http://www.mongodb.org/display/DOCS/Geospatial+Indexing

            :param name: Name of the indexed column
            :param min: minimum value for the index
            :param max: minimum value for the index
        """
        self.components.append((name, GEO2D))
        self.__min = min
        self.__max = max
        return self

    def geo_haystack(self, name, bucket_size):
        """ Create a Haystack index.  See:
            http://www.mongodb.org/display/DOCS/Geospatial+Haystack+Indexing

            :param name: Name of the indexed column
            :param bucket_size: Size of the haystack buckets (see mongo docs)   
        """
        self.components.append((name, 'geoHaystack'))
        self.__bucket_size = bucket_size
        return self

    def unique(self, drop_dups=False):
        ''' Make this index unique, optionally dropping duplicate entries.
                
            :param drop_dups: Drop duplicate objects while creating the unique \
                index?  Default to ``False``
        '''
        self.__unique = True
        self.__drop_dups = drop_dups
        return self
    
    def ensure(self, collection):
        ''' Call the pymongo method ``ensure_index`` on the passed collection.
            
            :param collection: the ``pymongo`` collection to ensure this index \
                    is on
        '''
        extras = {}
        if self.__min is not None:
            extras['min'] = self.__min
        if self.__max is not None:
            extras['max'] = self.__max
        if self.__bucket_size is not None:
            extras['bucket_size'] = self.__bucket_size
        collection.ensure_index(self.components, unique=self.__unique, 
            drop_dups=self.__drop_dups, **extras)
        return self
        
class Proxy(object):
    def __init__(self, name):
        self.name = name
    def __get__(self, instance, owner):
        if instance is None:
            return getattr(owner, self.name)
        session = instance._get_session()
        ref = getattr(instance, self.name)
        if ref is None:
            return None
        return session.dereference(ref)
    def __set__(self, instance, value):
        assert instance is not None
        setattr(instance, self.name, value)

class IProxy(object):
    def __init__(self, name):
        self.name = name
    def __get__(self, instance, owner):
        if instance is None:
            return getattr(owner, self.name)
        session = instance._get_session()
        def iterator():
            for v in getattr(instance, self.name):
                if v is None:
                    yield v
                    continue
                yield session.dereference(v)
        return iterator()
