"""@file correlatednoise.py

Python layer documentation and functions for handling correlated noise in GalSim.
"""

import numpy as np
import galsim
from . import base
from . import utilities

class _CorrFunc(object):
    """A class describing 2D correlation functions, typically calculated from Images.

    A CorrFunc will not generally be instantiated directly.  It defines the way in which derived
    classes (currently only the ImageCorrFunc) store the correlation function profile and generates
    images containing noise with these correlation properties.
    """
    def __init__(self, gsobject):
        if not isinstance(gsobject, base.GSObject):
            raise TypeError(
                "Correlation function objects must be initialized with a GSObject.")
        
        # Act as a container for the GSObject used to represent the correlation funcion.
        self.profile = gsobject

        # When applying noise to an image, we normally do a calculation. 
        # If store_profile is profile, then it means we can use the other stored values
        # and avoid having to redo the calculation.
        # So for now, we start out with store_profile = None.
        self.profile_for_stored = None

        # Delete some of the methods we don't want the use to have access to since they don't make
        # sense
        self.profile.applyShift = self._notImplemented
        self.profile.createShifted = self._notImplemented
        self.profile.applyDilation = self._notImplemented
        self.profile.createDilated = self._notImplemented
        self.profile.setFlux = self._notImplemented
        self.profile.getFlux = self._notImplemented
        self.profile.drawShoot = self._notImplemented # this isn't really needed
        
        # The statements below replace the self.profile.scaleFlux method with the much more
        # appropriately named self.profile.scaleVariance method
        self.profile.scaleVariance = self.scaleVariance
        
        # We use a similar pattern to replace the GSObject draw() method with a version that always
        # uses the "surface brightness" normalization rather than the default "flux"
        if not hasattr(self.profile, "_drawHidden"): # only do following once or becomes circular...
            self.profile._drawHidden = self.profile.draw # hide the method for internal use
        self.profile.draw = self.draw

        #
        self.profile.__idiv__ = self.__idiv__
        self.profile.__itruediv__ = self.__itruediv__
        self.profile.__imul__ = self.__imul__
        self.profile.__div__ = self.__div__
        self.profile.__truediv__ = self.__truediv__
        self.profile.__mul__ = self.__mul__

    # Make add work in the intuitive sense (variances being additive, correlation functions add as
    # you would expect)
    def __add__(self, other):
        ret = self.copy()
        ret += other
        return ret

    def __iadd__(self, other):
        self.profile += other.profile
        return _CorrFunc(self.profile)

    # Make op* and op*= work to adjust the overall variance of an object
    def __imul__(self, other):
        self.profile.scaleVariance(other)
        return self

    def __mul__(self, other):
        ret = self.copy()
        ret *= other
        return ret

    def __rmul__(self, other):
        ret = self.copy()
        ret *= other
        return ret

    # Likewise for op/ and op/=
    def __idiv__(self, other):
        self.profile.scaleVariance(1. / other)
        return self

    def __div__(self, other):
        ret = self.copy()
        ret /= other
        return ret

    def __itruediv__(self, other):
        return __idiv__(self, other)

    def __truediv__(self, other):
        return __div__(self, other)

    def copy(self):
        """Returns a copy of the correlation function.
        """
        return _CorrFunc(self.profile.copy())

    def applyNoiseTo(self, image, dx=0., dev=None, add_to_image=True):
        """Apply noise as a Gaussian random field with this correlation function to an input Image.

        If the optional image pixel scale `dx` is not specified, `image.getScale()` is used for the
        input image pixel separation.
        
        If an optional random deviate `dev` is supplied, the application of noise will share the
        same underlying random number generator when generating the vector of unit variance
        Gaussians that seed the (Gaussian) noise field.

        @param image The input Image object.
        @param dx    The pixel scale to adopt for the input image; should use the same units the
                     ImageCorrFunc instance for which this is a method.  If is specified,
                     `image.getScale()` is used instead.
        @param dev   Optional random deviate from which to draw pseudo-random numbers in generating
                     the noise field.
        @param add_to_image  Whether to add to the existing image rather than clear out anything
                             in the image before drawing.
                             Note: This requires that the image has defined bounds (default 
                             `add_to_image = True`).
        """
        # Note that this uses the (fast) method of going via the power spectrum and FFTs to generate
        # noise according to the correlation function represented by this instance.  An alternative
        # would be to use the covariance matrices and eigendecomposition.  However, it is O(N^6)
        # operations for an NxN image!  FFT-based noise realization is O(2 N^2 log[N]) so we use it
        # for noise generation applications.

        # Check that the input has defined bounds
        if not hasattr(image, "bounds"):
            raise ValueError(
                "Input image argument does not have a bounds attribute, it must be a galsim.Image"+
                "or galsim.ImageView-type object with defined bounds.")

        # Set up the Gaussian random deviate we will need later
        if dev is None:
            g = galsim.GaussianDeviate()
        else:
            if isinstance(dev, galsim.BaseDeviate):
                g = galsim.GaussianDeviate(dev)
            else:
                raise TypeError(
                    "Supplied input keyword dev must be a galsim.BaseDeviate or derived class "+
                    "(e.g. galsim.UniformDeviate, galsim.GaussianDeviate).")

        # If the profile has changed since last time (or if we have never been here before),
        # clear out the stored values.
        if self.profile_for_stored is not self.profile:
            self._rootps_store = []
        # Set profile_for_stored for next time.
        self.profile_for_stored = self.profile

        # Then retrieve or redraw the sqrt(power spectrum) needed for making the noise field:

        # First check whether we can just use the stored power spectrum (no drawing necessary if so)
        use_stored = False
        for rootps_array, scale in self._rootps_store:
            if image.array.shape == rootps_array.shape:
                if ((dx <= 0. and scale == 1.) or (dx == scale)):
                    use_stored = True
                    rootps = rootps_array
                    break

        # If not, draw the correlation function to the desired size and resolution, then DFT to
        # generate the required array of the square root of the power spectrum
        if use_stored is False:
            newcf = galsim.ImageD(image.bounds) # set the correlation func to be the correct size
            # set the scale based on dx...
            if dx <= 0.:
                if image.getScale() > 0.:
                    newcf.setScale(image.getScale())
                else:
                    newcf.setScale(1.) # sometimes new Images have getScale() = 0
            else:
                newcf.setScale(dx)
            # Then draw this correlation function into an array
            self.profile.draw(newcf, dx=None) # setting dx=None uses the newcf image scale set above

            # Roll to put the origin at the lower left pixel before FT-ing to get the PS...
            rolled_cf_array = utilities.roll2d(
                newcf.array, (-newcf.array.shape[0] / 2, -newcf.array.shape[1] / 2))

            # Then calculate the sqrt(PS) that will be used to generate the actual noise
            rootps = np.sqrt(np.abs(np.fft.fft2(newcf.array)) * np.product(image.array.shape))

            # Then add this and the relevant scale to the _rootps_store for later use
            self._rootps_store.append((rootps, newcf.getScale()))

        # Finally generate a random field in Fourier space with the right PS, and inverse DFT back,
        # including factor of sqrt(2) to account for only adding noise to the real component:
        gaussvec = galsim.ImageD(image.bounds)
        gaussvec.addNoise(g)
        noise_array = np.sqrt(2.) * np.fft.ifft2(gaussvec.array * rootps)
        # Make contiguous and add/assign to the image
        if add_to_image:
            image += galsim.ImageViewD(np.ascontiguousarray(noise_array.real))
        else:
            image = galsim.ImageViewD(np.ascontiguousarray(noise_array.real))
        return image

    def applyTransformation(self, ellipse):
        """Apply a galsim.Ellipse distortion to this correlation function.
           
        galsim.Ellipse objects can be initialized in a variety of ways (see documentation of this
        class, galsim.ellipse.Ellipse in the doxygen documentation, for details).

        @param ellipse The galsim.Ellipse transformation to apply.
        """
        if not isinstance(ellipse, galsim.Ellipse):
            raise TypeError("Argument to applyTransformation must be a galsim.Ellipse!")
        self.profile.applyTransformation(ellipse)

    def applyMagnification(self, scale):
        """Scale the linear size of this _CorrFunc by scale.  
        
        Scales the linear dimensions of the image by the factor scale, e.g.
        `half_light_radius` <-- `half_light_radius * scale`.

        @param scale The linear rescaling factor to apply.
        """
        self.applyTransformation(galsim.Ellipse(np.log(scale)))

    def applyRotation(self, theta):
        """Apply a rotation theta to this object.
           
        After this call, the caller's type will still be a _CorrFunc, unlike in the GSObject base
        class implementation of this method.  This is to allow _CorrFunc methods to be available
        after transformation, such as .applyNoiseTo().

        @param theta Rotation angle (Angle object, +ve anticlockwise).
        """
        if not isinstance(theta, galsim.Angle):
            raise TypeError("Input theta should be an Angle")
        self.profile.applyRotation(theta)

    def applyShear(self, *args, **kwargs):
        """Apply a shear to this object, where arguments are either a galsim.Shear, or arguments
        that will be used to initialize one.

        For more details about the allowed keyword arguments, see the documentation for galsim.Shear
        (for doxygen documentation, see galsim.shear.Shear).

        After this call, the caller's type will still be a _CorrFunc.  This is to allow _CorrFunc
        methods to be available after transformation, such as .applyNoiseTo().
        """
        self.profile.applyShear(*args, **kwargs)

    # Also add methods which create a new GSObject with the transformations applied...
    #
    def createTransformed(self, ellipse):
        """Returns a new correlation function by applying a galsim.Ellipse transformation (shear,
        dilate).

        Note that galsim.Ellipse objects can be initialized in a variety of ways (see documentation         of this class, galsim.ellipse.Ellipse in the doxygen documentation, for details).

        @param ellipse The galsim.Ellipse transformation to apply
        @returns The transformed object.
        """
        if not isinstance(ellipse, galsim.Ellipse):
            raise TypeError("Argument to createTransformed must be a galsim.Ellipse!")
        ret = self.copy()
        ret.applyTransformation(ellipse)
        return ret

    def createMagnified(self, scale):
        """Returns a new correlation function by applying a magnification by the given scale,
        scaling the linear size by scale.  
 
        Scales the linear dimensions of the image by the factor scale.
        e.g. `half_light_radius` <-- `half_light_radius * scale`
 
        @param scale The linear rescaling factor to apply.
        @returns The rescaled object.
        """
        ret = self.copy()
        ret.applyTransformation(galsim.Ellipse(np.log(scale)))
        return ret

    def createRotated(self, theta):
        """Returns a new correlation function by applying a rotation.

        @param theta Rotation angle (Angle object, +ve anticlockwise).
        @returns The rotated object.
        """
        if not isinstance(theta, galsim.Angle):
            raise TypeError("Input theta should be an Angle")
        ret = self.copy()
        ret.applyRotation(theta)
        return ret

    def createSheared(self, *args, **kwargs):
        """Returns a new correlation function by applying a shear, where arguments are either a
        galsim.Shear or keyword arguments that can be used to create one.

        For more details about the allowed keyword arguments, see the documentation of galsim.Shear
        (for doxygen documentation, see galsim.shear.Shear).
        """
        ret = self.copy()
        ret.applyShear(*args, **kwargs)
        return ret

    # Now I define some methods that are not used by this instance directly, but are used to
    # redefine the behaviour of the stored profile, or print a method saying that this method is not
    # implemented
    def scaleVariance(self, variance_ratio):
        """Multiply the overall variance of the correlation function profile by variance_ratio.

        @param variance_ratio The factor by which to scale the variance of the correlation function
                              profile.
        """
        self.profile.SBProfile.scaleFlux(variance_ratio)

    def _notImplemented(self, *args, **kwargs):
        raise NotImplementedError(
            "This method is not available for profiles that represent correlation functions.")

    def draw(self, image=None, dx=None, gain=1., wmult=1., add_to_image=False,
              normalization="surface brightness"):
        """The draw method for profiles storing correlation functions.

        This is a very mild reimplementation of the draw() method for GSObjects.  The normalization
        keyword argument default is changed to "surface brightness", as appropriate for this profile
        which is being used to store a correlation function.

        See the general GSObject draw() method for more information on the other parameters.
        """
        return self.profile._drawHidden(
            image=image, dx=dx, gain=gain, wmult=wmult, normalization=normalization,
            add_to_image=add_to_image)

###
# Then we define the ImageCorrFunc, which generates a correlation function by estimating it directly
# from images:
#
class ImageCorrFunc(_CorrFunc):
    """A class that represents 2D discrete correlation functions calculated from an input Image.

    Initialization
    --------------

    Basic example:

        >>> cf = galsim.ImageCorrFunc(image)

    Instantiates an ImageCorrFunc using the pixel scale information contained in image.getScale()
    (assumes the scale is unity if image.getScale() <= 0.)

    Optional Inputs
    ---------------

        >>> cf = galsim.ImageCorrFunc(image, dx=0.2)

    The example above instantiates an ImageCorrFunc, but forces the use of the pixel scale dx to
    set the units of the internal lookup table.

        >>> cf = galsim.ImageCorrFunc(image,
        ...     interpolant=galsim.InterpolantXY(galsim.Lanczos(5, tol=1.e-4))

    The example above instantiates a ImageCorrFunc, but forces the use of a non-default interpolant
    for interpolation of the internal lookup table.  Must be an InterpolantXY instance or an
    Interpolant instance (if the latter one-dimensional case is supplied an InterpolantXY will be
    automatically generated from it).

    The default interpolant if None is set is a galsim.InterpolantXY(galsim.Linear(tol=1.e-4)),
    which uses bilinear interpolation.  Initial tests indicate the favourable performance of this
    interpolant in applications involving correlated pixel noise.

    Attributes
    ----------
    The key attribue is `profile`, which is the internally stored GSObject profile.  It can be
    manipulated using many of the various methods of the GSObject.  The following are all legal:
    
        cf.profile.xValue(galsim.PositionD(0., 0.5))
        cf.profile.kValue(galsim.PositionD(0.5, 0.))
        cf.profile.drawK(im, dk)
        cf.profile.draw(im, dx)
        cf.profile.applyShear(s)
        cf.profile.applyMagnification(m)
        cf.profile.applyRotation(theta * galsim.degrees)
        cf.profile.applyTransformation(ellipse)

    A number of the GSObject functions have also been implemented directly as`cf` methods, so that
    the following commands are also all legal:

        cf.draw(im, dx, wmult=4)
        cf.createSheared(s)
        cf.createMagnified(m)
        cf.createRotated(theta * galsim.degrees)
        cf.createTransformed(ellipse)
        cf.applyShear(s)
        cf.applyMagnification(m)
        cf.applyRotation(theta * galsim.degrees)
        cf.applyTransformation(ellipse)

    However, some of the GSObject methods are not available, since they do not really make sense
    for correlation functions.  Calling any of

        cf.profile.applyShift
        cf.profile.createShifted
        cf.profile.applyDilation
        cf.profile.createDilated
        cf.profile.setFlux
        cf.profile.getFlux
        cf.profile.drawShoot
        cf.profile.scaleFlux

    will raise a `NotImplementedError`.  Note that while the `draw()` method is enabled, it has been
    reimplemented to make the default normalization keyword take the value 'surface brightness' as
    is appropriate for rendering correlation functions.

    A new method, which is in fact a more appropriately named reimplmentation of the
    `cf.profile.scaleFlux()` method, is

        cf.scaleVariance(variance_ratio)

    which scales the overall correlation function, and therefore its total variance, by a scalar
    factor `variance_ratio`.

    Arithmetic Operators
    --------------------
    Addition, multiplication and division operators are defined to work in an intuitive way for
    correlation functions.

    Addition works simply to add the correlation functions stored in the `.profile` attribute, so
    that

        >>> cf2 = cf0 + cf1
        >>> cf2 += cf1

    are functionally equivalent to

        >>> cf2.profile = cf0.profile + cf1.profile
        >>> cf2.profile += cf1.profile

    but slightly more convenient.  The multiplication and division operators scale the overall
    correlation function for scalar operands, using the `cf.profile.scaleVariance()` method.

    Methods
    -------
    The main way that ImageCorrFunc is used is to add or assign correlated noise to an image.
    This is done with

        cf.applyNoiseTo(im)

    The correlation function is calculated from its pixel values using the NumPy FFT functions.
    Optionally, the pixel scale for the input `image` can be specified using the `dx` keyword
    argument.  See the `applyNoiseTo` docstring for more information.

    If `dx` is not set the value returned by `image.getScale()` is used unless this is <= 0, in
    which case a scale of 1 is assumed.

    As already described, a number of the GSObject functions have also been implemented directly
    as`cf` methods.  See the 'Attributes' Section above.
    """
    def __init__(self, image, dx=0., interpolant=None):

        # Build a noise correlation function (CF) from the input image, using DFTs

        # Calculate the power spectrum then a (preliminary) CF 
        ft_array = np.fft.fft2(image.array)
        ps_array = np.abs(ft_array * ft_array.conj())
        cf_array_prelim = (np.fft.ifft2(ps_array)).real / float(np.product(np.shape(ft_array)))

        # Roll CF array to put the centre in image centre.  Remember that numpy stores data [y,x]
        cf_array_prelim = utilities.roll2d(
            cf_array_prelim, (cf_array_prelim.shape[0] / 2, cf_array_prelim.shape[1] / 2))

        # The underlying C++ object is expecting the CF to be represented by an odd-dimensioned 
        # array with the central pixel denoting the zero-distance correlation (variance), even 
        # even if the input image was even-dimensioned on one or both sides.
        # We therefore copy-paste and zero pad the CF calculated above to ensure that these
        # expectations are met. 
        #
        # Determine the largest dimension of the input image, and use it to generate an empty CF 
        # array for final output, padding by one to make odd if necessary:
        cf_array = np.zeros((
            1 + 2 * (cf_array_prelim.shape[0] / 2), 
            1 + 2 * (cf_array_prelim.shape[1] / 2))) # using integer division
        # Then put the data from the prelim CF into this array
        cf_array[0:cf_array_prelim.shape[0], 0:cf_array_prelim.shape[1]] = cf_array_prelim
        # Then copy-invert-paste data from the leftmost column to the rightmost column, and lowest
        # row to the uppermost row, if the the original CF had even dimensions in the x and y 
        # directions, respectively (remembering again that NumPy stores data [y,x] in arrays)
        if cf_array_prelim.shape[1] % 2 == 0: # first do x
            lhs_column = cf_array[:, 0]
            cf_array[:, cf_array_prelim.shape[1]] = lhs_column[::-1] # inverts order as required
        if cf_array_prelim.shape[0] % 2 == 0: # then do y
            bottom_row = cf_array[0, :]
            cf_array[cf_array_prelim.shape[0], :] = bottom_row[::-1] # inverts order as required

        # Store power spectrum and correlation function in an image 
        original_ps_image = galsim.ImageViewD(np.ascontiguousarray(ps_array))
        original_cf_image = galsim.ImageViewD(np.ascontiguousarray(cf_array))

        # Correctly record the original image scale if set
        if dx > 0.:
            original_cf_image.setScale(dx)
        elif image.getScale() > 0.:
            original_cf_image.setScale(image.getScale())
        else: # sometimes Images are instantiated with scale=0, in which case we will assume unit
              # pixel scale
            original_image.setScale(1.)
            original_cf_image.setScale(1.)

        # If interpolant not specified on input, use bilinear
        if interpolant == None:
            linear = galsim.Linear(tol=1.e-4)
            interpolant = galsim.InterpolantXY(linear)
        else:
            if isinstance(interpolant, galsim.Interpolant):
                interpolant = galsim.InterpolantXY(interpolant)
            elif isinstance(interpolant, galsim.InterpolantXY):
                interpolant = interpolant
            else:
                raise RuntimeError(
                    'Specified interpolant is not an Interpolant or InterpolantXY instance!')

        # Then initialize...
        _CorrFunc.__init__(self, base.InterpolatedImage(
            original_cf_image, interpolant, dx=original_cf_image.getScale(), normalization="sb"))

        # Finally store useful data as a (rootps, dx) tuple for efficient later use:
        self.profile_for_stored = self.profile
        self._rootps_store = []
        self._rootps_store.append(
            (np.sqrt(original_ps_image.array), original_cf_image.getScale()))

# Make a function for returning Noise correlations
def _Image_getCorrFunc(image):
    """Returns a CorrFunc instance by calculating the correlation function of image pixels.
    """
    return ImageCorrFunc(image)

# Then add this Image method to the Image classes
for Class in galsim.Image.itervalues():
    Class.getCorrFunc = _Image_getCorrFunc

for Class in galsim.ImageView.itervalues():
    Class.getCorrFunc = _Image_getCorrFunc

for Class in galsim.ConstImageView.itervalues():
    Class.getCorrFunc = _Image_getCorrFunc