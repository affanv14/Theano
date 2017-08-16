#section init_code_struct
prev_algo.algo = PARAMS->conv_algo;
prev_algo.mathType = CUDNN_DEFAULT_MATH;
prev_algo.dataType = CUDNN_DATA_FLOAT;
reuse_algo = 0;
hash_prefix = std::string("GI| GPU#");
#section support_code_struct

#line 12 "dnn_gi.c"
int     reuse_algo;
bool    use_cached;
AlgoRec prev_algo;
std::string hash_prefix;
  
int
APPLY_SPECIFIC(conv_gi)(PyGpuArrayObject *kerns, PyGpuArrayObject *output,
                        PyGpuArrayObject *im,
                        cudnnConvolutionDescriptor_t desc,
                        double alpha, double beta, PyGpuArrayObject **input,
                        PARAMS_TYPE* params) {
  PyGpuContextObject *c = kerns->context;
  void *alpha_p;
  void *beta_p;
  float af = alpha, bf = beta;
  cudnnStatus_t err = CUDNN_STATUS_SUCCESS;

  if (PyGpuArray_DIMS(im)[1] != PyGpuArray_DIMS(kerns)[1] * params->num_groups) {
    PyErr_SetString(PyExc_ValueError, "images and kernel must have the same "
                    "stack size");
    return 1;
  }
  if ((PyGpuArray_DIMS(kerns)[0] % params->num_groups) != 0) {
    PyErr_SetString(PyExc_ValueError,
		    "Number of filters must be divisible by number of groups");
    return 1;
  }

  switch (im->ga.typecode) {
  case GA_DOUBLE:
    alpha_p = (void *)&alpha;
    beta_p = (void *)&beta;
    break;
  case GA_FLOAT:
  case GA_HALF:
    alpha_p = (void *)&af;
    beta_p = (void *)&bf;
    break;
  default:
    PyErr_SetString(PyExc_TypeError, "Unsupported type in convolution");
    return 1;
  }

  if (params->inplace) {
    Py_XDECREF(*input);
    *input = im;
    Py_INCREF(*input);
  } else {
    if (theano_prep_output(input, PyGpuArray_NDIM(im), PyGpuArray_DIMS(im),
                           im->ga.typecode, GA_C_ORDER, c) != 0)
      return 1;
    if (beta != 0.0 && pygpu_move(*input, im))
      return 1;
  }

  if (PyGpuArray_DIMS(im)[0] == 0 || PyGpuArray_DIMS(kerns)[0] == 0 || PyGpuArray_DIMS(kerns)[1] == 0) {
    int err2 = GpuArray_memset(&(*input)->ga, 0);
    if (err2 != GA_NO_ERROR) {
        PyErr_Format(PyExc_RuntimeError,
                     "GpuDnnConv grad wrt. inputs could not fill the output with zeros: %d", err2);
        return 1;
    }
    return 0;
  }

  
  int groups = c_set_groups_for_conv(desc, params->num_groups);
  if (groups == -1)
    return 1;
  if (c_set_tensor_for_conv(output, APPLY_SPECIFIC(output), groups) == -1)
    return 1;
  if (c_set_filter(kerns, APPLY_SPECIFIC(kerns), groups) == -1)
    return 1;
  if (c_set_tensor_for_conv(*input, APPLY_SPECIFIC(input), groups) == -1)
    return 1;
  size_t input_offset = PyGpuArray_STRIDE(*input, 0) / groups;
  size_t kern_offset = PyGpuArray_STRIDE(kerns, 0) * PyGpuArray_DIM(kerns, 0) / groups;
  size_t output_offset = PyGpuArray_STRIDE(output, 0) / groups;

  cudnnConvolutionBwdDataAlgo_t algo = params->conv_algo;
  #ifdef DEBUG
  char algorithm_name[128];
  #endif
  size_t   worksize  = 0;
  cudnnMathType_t mathtype = CUDNN_DEFAULT_MATH;
  
  std::string hashkey;

  if (params->choose_algo && !reuse_algo) {
    char pci_id[16];
    gpucontext_property(c->ctx, GA_CTX_PROP_PCIBUSID, pci_id);    
    // check out cache
    hashkey=dnn_conv_shape(APPLY_SPECIFIC(input), *input, APPLY_SPECIFIC(kerns), kerns, desc, output, groups);
    if (hashkey.empty())
      return 1;
    hashkey = hash_prefix + pci_id + hashkey;
    const AlgoRec* cached = dnn_conv_check_cache(hashkey);
    if (cached) {
      prev_algo = *cached;
      use_cached = 1;
    }
  }

  size_t free = c_get_largest_free_block_size(c);
    
  cuda_enter(c->ctx);
  
  if (params->choose_algo && !(reuse_algo || use_cached)) {
    if (params->choose_time) {
      int count;
      cudnnConvolutionBwdDataAlgoPerf_t choice;
      gpudata *tmpmem;

      tmpmem = gpudata_alloc(c->ctx, free, NULL, 0, NULL);
      if (tmpmem == NULL) {
        PyErr_SetString(PyExc_MemoryError, "Could not allocate working GPU memory");
        return -1;
      }

      err = cudnnFindConvolutionBackwardDataAlgorithmEx(
        params->handle, APPLY_SPECIFIC(kerns), PyGpuArray_DEV_DATA(kerns),
        APPLY_SPECIFIC(output), PyGpuArray_DEV_DATA(output), desc,
        APPLY_SPECIFIC(input), PyGpuArray_DEV_DATA(*input),
        1, &count, &choice, *(void **)tmpmem, free);
      gpudata_release(tmpmem);

      if (err != CUDNN_STATUS_SUCCESS) {
        PyErr_Format(PyExc_RuntimeError, "error selecting convolution algo: %s",
                     cudnnGetErrorString(err));
        cuda_exit(c->ctx);
        return 1;
      }

      algo = choice.algo;
        prev_algo.algo = (int)algo;
        prev_algo.wsSize = worksize = choice.memory;
#if CUDNN_MAJOR >= 7
        prev_algo.mathType = mathtype = choice.mathType;
#endif
        // Add to the cache
	dnn_conv_update_cache(hashkey, prev_algo);

      #ifdef DEBUG
      if (count == 0) {
          PyErr_SetString(PyExc_RuntimeError, "No best-timed conv gradinput algorithm found");
          return 1;
      } else if (choice.status != CUDNN_STATUS_SUCCESS) {
          PyErr_Format(PyExc_RuntimeError,
                       "error getting best-timed gradinput algo: %s",
                       cudnnGetErrorString(choice.status));
          return 1;
      } // Else, count is necessarly 1 for current implementation.
      #endif

    } else {
      err = cudnnGetConvolutionBackwardDataAlgorithm(
        params->handle, APPLY_SPECIFIC(kerns), APPLY_SPECIFIC(output),
        desc, APPLY_SPECIFIC(input),
        CUDNN_CONVOLUTION_BWD_DATA_SPECIFY_WORKSPACE_LIMIT, free, &algo);
      if (err != CUDNN_STATUS_SUCCESS) {
        PyErr_Format(PyExc_RuntimeError, "error selecting convolution algo: %s",
                     cudnnGetErrorString(err));
        cuda_exit(c->ctx);
        return 1;
      }
      prev_algo.algo = algo;
      // no tensor_op returned from Get()
      prev_algo.mathType = mathtype = CUDNN_DEFAULT_MATH;
    }
  }
  
  // if FindEx was used (choose_time), workspace size is set. 
  if (!(reuse_algo || use_cached || params->choose_time))
  {
    
    err = cudnnGetConvolutionBackwardDataWorkspaceSize(
    params->handle, APPLY_SPECIFIC(kerns), APPLY_SPECIFIC(output), desc,
    APPLY_SPECIFIC(input), algo, &worksize);

    if (err != CUDNN_STATUS_SUCCESS) {
      PyErr_Format(PyExc_RuntimeError, "error getting worksize: %s",
                   cudnnGetErrorString(err));

      // The FFT implementation does not support strides, 1x1 filters or inputs
      // with a spatial dimension larger than 1024. The tiled-FFT implementation
      // does not support strides.
      // If the chosen implementation is FFT or tiled-FFT, validate that it can
      // be used on the current data and default to a safe implementation if it
      // can't.
      // The following code is 2d-specific but it is fine as FFT and tiled-FFT are
      // defined only for 2d filters
      if ((algo == CUDNN_CONVOLUTION_BWD_DATA_ALGO_FFT_TILING ||
           algo == CUDNN_CONVOLUTION_BWD_DATA_ALGO_FFT) && PyGpuArray_NDIM(kerns) == 4) {
        
        // Extract the properties of the convolution descriptor
        int nd;
        int pad[2];
        int stride[2];
        int upscale[2];
        cudnnConvolutionMode_t mode;
        cudnnDataType_t data_type;
        err = cudnnGetConvolutionNdDescriptor(desc, 2, &nd, pad, stride,
                                              upscale, &mode, &data_type);
        if (err != CUDNN_STATUS_SUCCESS) {
          PyErr_Format(PyExc_RuntimeError,
                       "error getting convolution properties: %s",
                       cudnnGetErrorString(err));
          cuda_exit(c->ctx);
          return 1;
        }
        
        if (algo == CUDNN_CONVOLUTION_BWD_DATA_ALGO_FFT)
        {
          if (stride[0] != 1 || stride[1] != 1 ||
              PyGpuArray_DIM(*input, 2) > 1024 || PyGpuArray_DIM(*input, 3) > 1024 ||
              (PyGpuArray_DIM(kerns, 2) == 1 && PyGpuArray_DIM(kerns, 3) == 1))
          {
            algo = CUDNN_CONVOLUTION_BWD_DATA_ALGO_0;
          }
        }
        else
        {
          // algo == CUDNN_CONVOLUTION_BWD_DATA_ALGO_FFT_TILING
          if (stride[0] != 1 || stride[1] != 1)
          {
            algo = CUDNN_CONVOLUTION_BWD_DATA_ALGO_0;
          }
        }
      }
      err = cudnnGetConvolutionBackwardDataWorkspaceSize(
        params->handle, APPLY_SPECIFIC(kerns), APPLY_SPECIFIC(output), desc,
        APPLY_SPECIFIC(input), algo, &worksize);
    }
    
    if (err != CUDNN_STATUS_SUCCESS) {      
      cuda_exit(c->ctx);
      return 1;
    }
    // save worksize for next time/cache
    prev_algo.wsSize = worksize;
    
    // Add to the cache
    if (params->choose_algo)
      dnn_conv_update_cache(hashkey, prev_algo);
  }  // !(reuse_algo || use_cached || params->choose_time)

#ifdef DEBUG
  if (params->choose_algo) { 
    if (0 != theano_enum_to_string_cudnnConvolutionBwdDataAlgo_t(algo, algorithm_name))
        return 1;
    // NB: This is printed only when algorithm is chosen at runtime.
    fprintf(stderr, "%s%s algo: %d %s%s ws: %ld, tensor: %d hash:%s\n",
            params->choose_algo ? "[A]": "" ,
            params->choose_time ? "[T]": "" ,
            algo, // algorithm_name,
            reuse_algo ? "(reused)" : "",
            use_cached ? "(cache)": "",
            worksize, mathtype, hashkey.c_str()
      );
  }
#endif

    if (params->choose_once) {
      reuse_algo = 1;
    }
    
    gpudata *workspace = 0;  
#if CUDNN_MAJOR >= 7    
    // CUDNN7: need to set math type
    err = cudnnSetConvolutionMathType(desc, prev_algo.mathType);
    if (err != CUDNN_STATUS_SUCCESS) {
      PyErr_Format(PyExc_RuntimeError,
                   "error setting math type for convolution : %s",
                   cudnnGetErrorString(err));
      cuda_exit(c->ctx);
      return 1;
    }
#endif
    
  if (worksize != 0) {
    workspace = gpudata_alloc(c->ctx, worksize, NULL, 0, NULL);
    if (workspace == NULL) {
      PyErr_SetString(PyExc_RuntimeError,
                      "Could not allocate working memory");
      cuda_exit(c->ctx);
      return 1;
    }
  }

  cuda_wait(kerns->ga.data, GPUARRAY_CUDA_WAIT_READ);
  cuda_wait(output->ga.data, GPUARRAY_CUDA_WAIT_READ);
  cuda_wait((*input)->ga.data, GPUARRAY_CUDA_WAIT_WRITE);

  for ( int g = 0; g < groups; g++)
  {
    err = cudnnConvolutionBackwardData(
      params->handle,
      alpha_p,
      APPLY_SPECIFIC(kerns), ((char *)PyGpuArray_DEV_DATA(kerns)) + kern_offset * g,
      APPLY_SPECIFIC(output), ((char *)PyGpuArray_DEV_DATA(output)) + output_offset * g,
      desc, algo, worksize == 0 ? NULL : *(void **)workspace, worksize,
      beta_p,
      APPLY_SPECIFIC(input), ((char *)PyGpuArray_DEV_DATA(*input)) + input_offset * g);
  }

  if (worksize != 0)
    gpudata_release(workspace);

  cuda_record(kerns->ga.data, GPUARRAY_CUDA_WAIT_READ);
  cuda_record(output->ga.data, GPUARRAY_CUDA_WAIT_READ);
  cuda_record((*input)->ga.data, GPUARRAY_CUDA_WAIT_WRITE);

  cuda_exit(c->ctx);

  if (err != CUDNN_STATUS_SUCCESS) {
    PyErr_Format(PyExc_RuntimeError, "error doing operation: %s",
                 cudnnGetErrorString(err));
    return 1;
  }
  return 0;
}
